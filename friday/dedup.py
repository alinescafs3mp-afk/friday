"""Near-duplicate Knowledge Object detection over persisted embeddings.

Entity resolution deduplicates entities; nothing deduplicates knowledge itself,
so a bulk import (bookmarks, mail) inevitably accumulates near-identical
records. This reuses the vectors already indexed for dense recall: pairwise
cosine above a high threshold flags a likely duplicate, stored as a review-gated
``near_duplicate`` conflict — so the existing "keep A / keep B" resolution
(§5) IS the deduplication (the loser becomes ``deprecated``). Nothing is merged
or deleted automatically.

The scan is INCREMENTAL. All-pairs over a capped window was O(n²) every run, which
is why the window existed — and the window was the defect: an object older than it
was never a comparison partner, so a duplicate of it arriving later was invisible
forever, mentioned only in a log line. Instead each object is probed against the
WHOLE corpus exactly once: newly indexed vectors first, then a descending backfill
that pays off history a tile per tick. The cursor lives in ``runtime_kv``, so a
scan resumes across restarts, and the budget stopped meaning "silently miss" and
started meaning "catch up over the next few ticks".
"""

from __future__ import annotations

import heapq
import json
import logging
import math
import time
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, replace
from typing import Any

from friday.retrieval import pack_vector, unpack_vector
from friday.storage.models import utc_now

try:  # optional acceleration (jericho[vectors]); pure-Python fallback below
    import numpy as _np
except ImportError:  # pragma: no cover - exercised only when numpy is absent
    _np = None  # type: ignore[assignment]

LOGGER = logging.getLogger(__name__)

NEAR_DUPLICATE_TYPE = "near_duplicate"

# Rows per corpus page — the memory fuse on BOTH paths.
_CORPUS_PAGE = 2048
# Preserves today's arrival rate into the review queue (was find_near_duplicate_pairs'
# max_pairs default). A module constant, not a setting: nobody would turn this dial.
_MAX_PAIRS_PER_RUN = 200
# Highest cosine MEASURED between two documents that are not duplicates, on the
# installed model at document scale (`tools/dedup_threshold_probe.py`, 20 pairs of
# Russian notes of the length this installation stores). It comes from two weekly
# meeting notes written to the same template — same project, same attendees, same
# headings, one differing line. Two entries about one apartment scored 0.917 and
# 0.914; a Monday and a Tuesday log, 0.779.
#
# Genuine duplicates span 0.683 (the same event described independently) to 1.000,
# so the two classes OVERLAP and no threshold separates them. Adding lexical overlap
# does not help: the templated pair scores 0.845 there against 0.632 for a genuine
# reformatting. Whole-document similarity cannot tell "the same note again" from
# "the next note in a series", because a series is textually near-identical by
# construction.
#
# The consequence for the threshold: this detector can only be operated for
# precision. Any value that reaches the reformatted or retold classes first proposes
# merging the owner's weekly minutes.
_MEASURED_NON_DUPLICATE_CEILING = 0.928
# What storage.list_user_vectors_page will actually honour; a bigger request is
# silently trimmed there, and a short page must never be read as end-of-history.
_MAX_PAGE_ROWS = 10000
# Scan cursor lives in runtime_kv (already in CORE_SCHEMA) — no migration, no bump.
_SCAN_STATE_PREFIX = "dedup:scan:"
_SCAN_STATE_VERSION = 1
# Statuses a human has settled: re-proposing these would be nagging, not detection.
# 'confirmed' is deliberately absent — re-touching it only raises confidence.
_SETTLED = frozenset({"dismissed", "resolved"})


def _unit(vector: list[float]) -> list[float] | None:
    norm = math.sqrt(sum(value * value for value in vector))
    if norm <= 0.0:
        return None
    return [value / norm for value in vector]


def _units_by_dim(rows: Sequence[tuple[str, bytes]]) -> dict[int, list[tuple[str, list[float]]]]:
    """Bucket packed vectors by dimension, dropping zero-norm rows.

    Both compare paths start here, so both reproduce exactly the two skips the
    original pairwise loop had: a zero vector is not comparable, and vectors of
    different length are never compared to each other.
    """
    buckets: dict[int, list[tuple[str, list[float]]]] = {}
    for object_id, blob in rows:
        values = list(unpack_vector(blob))
        unit = _unit(values)
        if unit is None:
            continue
        buckets.setdefault(len(unit), []).append((object_id, unit))
    return buckets


def _pairs_against_python(
    probes: Sequence[tuple[str, bytes]],
    corpus: Sequence[tuple[str, bytes]],
    *,
    threshold: float,
    upper_triangle: bool,
) -> Iterator[tuple[str, str, float]]:
    probe_buckets = _units_by_dim(probes)
    corpus_buckets = _units_by_dim(corpus)
    for dim, probe_rows in probe_buckets.items():
        for probe_id, probe_vec in probe_rows:
            for corpus_id, corpus_vec in corpus_buckets.get(dim, ()):
                if corpus_id <= probe_id if upper_triangle else corpus_id == probe_id:
                    continue
                score = sum(a * b for a, b in zip(probe_vec, corpus_vec, strict=False))
                if score >= threshold:
                    low, high = sorted((probe_id, corpus_id))
                    yield low, high, score


def _pairs_against_numpy(
    probes: Sequence[tuple[str, bytes]],
    corpus: Sequence[tuple[str, bytes]],
    *,
    threshold: float,
    upper_triangle: bool,
) -> Iterator[tuple[str, str, float]]:
    probe_buckets = _units_by_dim(probes)
    corpus_buckets = _units_by_dim(corpus)
    for dim, probe_rows in probe_buckets.items():
        corpus_rows = corpus_buckets.get(dim)
        if not corpus_rows:
            continue
        # float64, matching the pure-Python accumulator exactly: float32 sgemm drifts
        # by ~5e-7, which is enough to put a pair on the other side of the threshold
        # and make the optional extra report a DIFFERENT set of duplicates.
        probe_matrix = _np.asarray([vector for _, vector in probe_rows], dtype=_np.float64)
        corpus_matrix = _np.asarray([vector for _, vector in corpus_rows], dtype=_np.float64)
        # Row by row so only ONE probe's hits are ever materialised: a duplicate
        # cluster otherwise builds a multi-million-entry index array up front.
        for row_index, probe_scores in enumerate(probe_matrix @ corpus_matrix.T):
            probe_id = probe_rows[row_index][0]
            for column in _np.nonzero(probe_scores >= threshold)[0]:
                corpus_id = corpus_rows[int(column)][0]
                if corpus_id <= probe_id if upper_triangle else corpus_id == probe_id:
                    continue
                low, high = sorted((probe_id, corpus_id))
                yield low, high, float(probe_scores[column])


def find_near_duplicate_pairs_against(
    probes: Sequence[tuple[str, bytes]],
    corpus: Sequence[tuple[str, bytes]],
    *,
    threshold: float,
    upper_triangle: bool = False,
) -> Iterator[tuple[str, str, float]]:
    """Yield canonical ``(low, high, score)`` for every probe-vs-corpus match.

    A GENERATOR on purpose: a duplicate cluster produces a quadratic number of hits,
    and materialising them was measured at 332 MB for one 512-probe tile against 5000
    identical vectors. The consumer keeps only the strongest few.

    ``upper_triangle`` is for comparing a set against ITSELF: without it every
    unordered pair is yielded twice (once from each side). Self-pairs are always
    suppressed, since a probe is normally also a member of the corpus it is scanned
    against. Both paths take PACKED bytes and accumulate in float64, so they agree.
    """
    if not probes or not corpus:
        return iter(())
    if _np is not None:
        return _pairs_against_numpy(probes, corpus, threshold=threshold, upper_triangle=upper_triangle)
    return _pairs_against_python(probes, corpus, threshold=threshold, upper_triangle=upper_triangle)


def find_near_duplicate_pairs(
    vectors: list[tuple[str, list[float]]],
    *,
    threshold: float,
    max_pairs: int = _MAX_PAIRS_PER_RUN,
) -> list[tuple[str, str, float]]:
    """Return (id_a, id_b, cosine) pairs at or above ``threshold``, strongest first.

    ``id_a`` < ``id_b`` canonically so a pair is reported once regardless of order.
    Zero and mismatched-length vectors are skipped rather than compared. Kept as a
    thin all-pairs wrapper over the probe-vs-corpus kernel; the incremental scanner
    calls the kernel directly.
    """
    packed = [(object_id, pack_vector(vector)) for object_id, vector in vectors]
    pairs = {
        (low, high): score
        for low, high, score in find_near_duplicate_pairs_against(packed, packed, threshold=threshold)
    }
    # Deterministic tie-break on equal scores, which the bare reverse sort lacked.
    ordered = sorted(pairs.items(), key=lambda item: (-item[1], item[0][0], item[0][1]))
    return [(low, high, score) for (low, high), score in ordered[:max_pairs]]


@dataclass(frozen=True)
class ScanState:
    """Where the incremental scan got to. Values only — never a row reference, so a
    purged object named by a cursor breaks nothing."""

    model: str = ""
    threshold: float = 0.0
    watermark: tuple[str, str] | None = None
    backfill: tuple[str, str] | None = None
    backfill_done: bool = False
    # Rows below the watermark when history was finished; a later increase means a row
    # appeared underneath the cursor and the backfill has to reopen.
    swept_below: int | None = None


def _merge_concurrent(
    raw_before: str | None, state: ScanState, *, restart_backfill: bool = False
) -> ScanState:
    """Fold this run's progress into whatever is stored NOW, conservatively.

    The state is a read-modify-write of one blob spanning the whole run, and the worker
    tick and the manual admin scan can overlap. Rather than a lease, resolve it so the
    loser can only cost extra work, never coverage: take the further cursor (both runs
    genuinely swept it), and declare history finished only if BOTH runs agree.

    ``raw_before`` must be re-read immediately before saving, NOT the snapshot the
    run started from. Merging against your own past turns «both agree» into «I
    agree with myself as I was»: once a single tick ended with
    ``backfill_done=False`` — which any corpus larger than one tick's budget does
    — no later run could ever record history as finished, because the old value
    it was comparing against always said False.

    ``restart_backfill`` says «start history again from the top», which cannot be
    expressed as ``backfill=None``: the merge drops falsy cursors and would hand
    back the stored deep one, leaving the scan asking for rows below the bottom
    of the corpus and calling that finished.
    """
    if raw_before is None:
        return state
    try:
        stored = _decode_scan_state(raw_before)
    except (ValueError, TypeError, json.JSONDecodeError):
        return state
    if restart_backfill:
        return replace(
            state,
            watermark=max(filter(None, (state.watermark, stored.watermark)), default=None),
            backfill=state.backfill,
            backfill_done=False,
            swept_below=None,
        )
    return replace(
        state,
        watermark=max(filter(None, (state.watermark, stored.watermark)), default=None),
        backfill=min(filter(None, (state.backfill, stored.backfill)), default=None),
        backfill_done=state.backfill_done and stored.backfill_done,
        swept_below=state.swept_below if state.backfill_done and stored.backfill_done else None,
    )


def _as_cursor(value: Any) -> tuple[str, str] | None:
    if isinstance(value, (list, tuple)) and len(value) == 2:
        return (str(value[0]), str(value[1]))
    return None


def _decode_scan_state(raw: str) -> ScanState:
    parsed = json.loads(raw)
    if not isinstance(parsed, dict) or int(parsed.get("version", 0)) != _SCAN_STATE_VERSION:
        return ScanState()
    swept_below = parsed.get("swept_below")
    return ScanState(
        model=str(parsed.get("model", "")),
        threshold=float(parsed.get("threshold", 0.0)),
        watermark=_as_cursor(parsed.get("watermark")),
        backfill=_as_cursor(parsed.get("backfill")),
        backfill_done=bool(parsed.get("backfill_done", False)),
        swept_below=None if swept_below is None else int(swept_below),
    )


def load_scan_state(storage: Any, user_id: str) -> ScanState:
    raw = storage.kv_get(f"{_SCAN_STATE_PREFIX}{user_id}")
    if not raw:
        return ScanState()
    try:
        return _decode_scan_state(raw)
    except (ValueError, TypeError, json.JSONDecodeError):
        # A corrupt marker must cost a rescan, never a crashed worker tick.
        return ScanState()


def save_scan_state(storage: Any, user_id: str, state: ScanState) -> None:
    storage.kv_set(
        f"{_SCAN_STATE_PREFIX}{user_id}",
        json.dumps(
            {
                "version": _SCAN_STATE_VERSION,
                "model": state.model,
                "threshold": state.threshold,
                "watermark": list(state.watermark) if state.watermark else None,
                "backfill": list(state.backfill) if state.backfill else None,
                "backfill_done": state.backfill_done,
                "swept_below": state.swept_below,
            },
            ensure_ascii=False,
            sort_keys=True,
        ),
    )


class _PairCollector:
    """Keeps only the strongest ``_MAX_PAIRS_PER_RUN`` candidate pairs.

    Retaining every above-threshold pair is quadratic in the size of a duplicate
    cluster — and a bulk import of near-identical records is precisely the workload
    this module exists for. Measured: one 512-probe tile against 5000 identical
    vectors yields 2.56M pairs and 332 MB before a single row is written, when at most
    200 of them can ever be stored. Pairs a human already settled are dropped here
    rather than after collection, so a settled cluster cannot crowd out a genuinely
    new duplicate found later in the same run.
    """

    def __init__(self, settled: dict[str, str]) -> None:
        self._settled = settled
        self._heap: list[tuple[float, str, str]] = []
        self._best: dict[tuple[str, str], float] = {}
        self.total = 0
        self.suppressed = 0

    def add(self, low: str, high: str, score: float) -> None:
        if self._settled.get(f"{low}|{high}") in _SETTLED:
            self.suppressed += 1
            return
        previous = self._best.get((low, high))
        if previous is not None:
            if score <= previous:
                return
            self._best[(low, high)] = score
            # Filtering a heap with a comprehension leaves a plain list, not a heap:
            # removing an interior element breaks the invariant, and the very next
            # ``heappush`` assumes it holds. ``_heap[0]`` then stops being the minimum
            # and ``heappushpop`` below evicts a pair that is not the weakest —
            # reproduced as 116 of 282 evictions discarding a stronger candidate than
            # the arrival that displaced it. Re-heapify; a re-score is rare and O(n)
            # here is nothing next to the cosine that produced the score.
            heapq.heapify(self._heap)
            heapq.heappush(self._heap, (score, low, high))
            return
        self.total += 1
        if len(self._heap) < _MAX_PAIRS_PER_RUN:
            self._best[(low, high)] = score
            heapq.heappush(self._heap, (score, low, high))
            return
        if score <= self._heap[0][0]:
            return
        _, weak_low, weak_high = heapq.heappushpop(self._heap, (score, low, high))
        self._best.pop((weak_low, weak_high), None)
        self._best[(low, high)] = score

    def ranked(self) -> list[tuple[str, str, float]]:
        return [
            (low, high, score)
            for score, low, high in sorted(self._heap, key=lambda item: (-item[0], item[1], item[2]))
        ]

    @property
    def dropped(self) -> bool:
        """True when more candidates were seen than can be reported this run."""
        return self.total > len(self._heap)


def _sweep_probes(
    storage: Any,
    user_id: str,
    model: str,
    tile: list[tuple[str, str, bytes]],
    *,
    collector: _PairCollector,
    threshold: float,
    max_updated_at: str,
    below: tuple[str, str] | None,
    deadline: float,
    allow_interrupt: bool,
) -> bool:
    """Compare one tile of probes against the corpus, page by page.

    ``below`` restricts the sweep to rows strictly below the tile's lowest member —
    used by the descending backfill, where everything above has already been a probe.
    Returns whether the sweep completed; an interrupted sweep keeps the pairs it found
    (they are real) but its cursor must not advance.

    ``allow_interrupt`` is False for the first tile of a run. The page cursor is local,
    so an interrupted sweep is discarded and replayed identically next tick — if one
    tile against the corpus costs more than a whole tick budget, nothing would ever
    complete and the scan would make zero forward progress forever. Guaranteeing one
    completed tile per run trades a possible budget overrun for guaranteed progress.
    """
    probes = [(object_id, blob) for object_id, _, blob in tile]
    probe_ids = {object_id for object_id, _ in probes}
    # The tile against itself: a duplicate can live entirely inside one tile.
    for low, high, score in find_near_duplicate_pairs_against(
        probes, probes, threshold=threshold, upper_triangle=True
    ):
        collector.add(low, high, score)
    cursor: tuple[str, str] | None = None
    while True:
        page = storage.list_user_vectors_page(
            user_id,
            model,
            after=cursor,
            before=below,
            max_updated_at=max_updated_at,
            limit=_CORPUS_PAGE,
        )
        if not page:
            return True
        # Tile members are corpus rows too; the self-comparison above already covered
        # every pair among them, so re-comparing them here would only re-emit the same
        # pair (twice more) and inflate the candidate count.
        corpus = [(object_id, blob) for object_id, _, blob in page if object_id not in probe_ids]
        for low, high, score in find_near_duplicate_pairs_against(probes, corpus, threshold=threshold):
            collector.add(low, high, score)
        cursor = (page[-1][1], page[-1][0])
        if len(page) < _CORPUS_PAGE:
            return True
        # Checked BETWEEN pages: a tile is never abandoned half-swept mid-page.
        if allow_interrupt and time.monotonic() >= deadline:
            return False


def detect_near_duplicates(
    storage: Any,
    settings: Any,
    user_id: str,
    *,
    threshold: float | None = None,
    max_seconds: float | None = None,
    full_rescan: bool = False,
) -> dict[str, Any]:
    """Compare newly indexed vectors against the WHOLE corpus, resuming across ticks.

    The old scan took the newest N vectors and compared them to each other, so an
    object older than the window was never a comparison partner and a duplicate of it
    arriving later was invisible — permanently, and only ever mentioned in a log line.
    Now every object is probed against the entire corpus exactly once: new objects
    first (fresh data beats old debt), then a descending backfill that pays off the
    history a tile at a time. The cap stopped meaning "silently miss" and started
    meaning "catch up over the next few ticks".
    """
    model = settings.embeddings_model
    if not model:
        return {"detected": 0, "reason": "embeddings model not configured"}

    started = time.monotonic()
    # Rows written in the CURRENT second may not all be there yet, and second-precision
    # timestamps make an exact cursor unsafe across that boundary: a row written later
    # in the same second would sort below the watermark and be skipped forever. Bound
    # every read to strictly-closed seconds instead; those rows are swept next run.
    run_started_at = utc_now()
    budget = float(max_seconds if max_seconds is not None else settings.dedup_scan_max_seconds)
    deadline = started + max(0.0, budget)
    effective_threshold = float(threshold if threshold is not None else settings.dedup_threshold)
    # Clamped to what the storage page query will actually honour: a larger request is
    # silently trimmed there, and a short page must never be read as end-of-history.
    batch = max(1, min(int(settings.dedup_scan_batch), _MAX_PAGE_ROWS))

    state = load_scan_state(storage, user_id)
    # A LOWER threshold makes previously rejected pairs eligible while no vector moved,
    # so the walk has to start over. Raising it needs no rescan.
    if full_rescan or state.model != model or effective_threshold < state.threshold - 1e-9:
        # A fresh walk starts at the TOP: anything already stored is history for the
        # backfill to pay off, so genuinely new objects are never stuck behind a large
        # existing corpus waiting their turn.
        top = storage.list_user_vectors_page(
            user_id, model, max_updated_at=run_started_at, descending=True, limit=1
        )
        state = ScanState(
            model=model,
            threshold=effective_threshold,
            watermark=(top[0][1], top[0][0]) if top else None,
            backfill_done=not top,
        )

    # The watermark this run STARTED from. The guard below asks "did a row appear
    # UNDER the cursor?", and `state.swept_below` was counted against this value — but
    # the incremental sweep pushes the cursor up as it goes, so counting against the
    # advanced one compared two different quantities. Every ordinary new object then
    # made the count grow by exactly one and looked like a row that had landed below.
    entry_watermark = state.watermark

    # Loaded BEFORE the sweep so settled pairs are dropped as they are found, never
    # occupying a slot a genuinely new duplicate could use.
    settled = storage.get_conflict_pair_statuses(user_id, NEAR_DUPLICATE_TYPE)
    collector = _PairCollector(settled)
    probed = 0
    complete = True
    mode = "incremental"
    while True:
        tile = storage.list_user_vectors_page(
            user_id,
            model,
            after=state.watermark,
            max_updated_at=run_started_at,
            limit=batch,
        )
        below: tuple[str, str] | None = None
        if tile:
            mode = "incremental"
        elif not state.backfill_done:
            mode = "backfill"
            tile = storage.list_user_vectors_page(
                user_id,
                model,
                before=state.backfill,
                max_updated_at=run_started_at,
                descending=True,
                limit=batch,
            )
            if not tile:
                state = replace(state, backfill_done=True)
                break
            below = (tile[-1][1], tile[-1][0])
        else:
            break

        swept = _sweep_probes(
            storage,
            user_id,
            model,
            tile,
            collector=collector,
            threshold=effective_threshold,
            max_updated_at=run_started_at,
            below=below,
            deadline=deadline,
            # The first tile of a run always finishes: otherwise a tile that costs more
            # than one budget could never complete and the scan would stall forever.
            allow_interrupt=probed > 0,
        )
        if not swept:
            # Cursor stays put: the tile is replayed next tick rather than skipped.
            complete = False
            break
        probed += len(tile)
        if mode == "incremental":
            state = replace(state, watermark=(tile[-1][1], tile[-1][0]))
        else:
            # End of history is inferred ONLY from an empty page (handled above). A
            # short page must not mean "done": the storage query silently trims an
            # oversized limit, which would otherwise declare history finished after a
            # single tile and leave everything older permanently unprobed.
            state = replace(state, backfill=below)
        if time.monotonic() >= deadline:
            complete = False
            break

    ordered = collector.ranked()
    if collector.dropped:
        complete = False
    stored = 0
    for id_a, id_b, score in ordered:
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

    corpus_size = storage.count_user_vectors(user_id, model)
    reopened_backfill = False
    if state.backfill_done:
        # A wall-clock step backwards (or any future path that writes a row with an
        # older stamp) can land a row BELOW the watermark, where the strict `after`
        # bound would hide it from probing forever. Cheap detection: remember how many
        # rows were below the watermark when history was finished, and reopen the
        # backfill if that number ever grows.
        at_entry = storage.count_user_vectors(user_id, model, before=entry_watermark)
        if state.swept_below is not None and at_entry > state.swept_below:
            LOGGER.info(
                "Near-duplicate scan for %s found %d row(s) below the watermark; reopening backfill",
                user_id,
                at_entry - state.swept_below,
            )
            state = replace(state, backfill=None, backfill_done=False, swept_below=None)
            reopened_backfill = True
        else:
            # Recorded against the watermark this run LEAVES, which is what the next
            # run will enter with — so the two ends of the comparison always agree.
            state = replace(
                state, swept_below=storage.count_user_vectors(user_id, model, before=state.watermark)
            )
    pending = 0 if state.backfill_done else storage.count_user_vectors(user_id, model, before=state.backfill)
    if probed == 0 and not complete:
        # Distinguishable from "nothing to do": a run that burned its whole budget and
        # advanced nothing is a stall, not a quiet success.
        LOGGER.warning(
            "Near-duplicate scan for %s made no progress within its budget (%d object(s) pending)",
            user_id,
            pending,
        )
    if threshold is None:
        # A one-off admin threshold experiment must not move the persistent cursor.
        # Re-read immediately before writing: merging against the snapshot this
        # run STARTED from compares the run with its own past, and «both agree»
        # then means «I agree with myself as I was».
        save_scan_state(
            storage,
            user_id,
            _merge_concurrent(
                storage.kv_get(f"{_SCAN_STATE_PREFIX}{user_id}"),
                replace(state, model=model, threshold=effective_threshold),
                restart_backfill=reopened_backfill,
            ),
        )
    return {
        "detected": stored,
        # Above-threshold pairs seen this run. A pair whose two sides fall in different
        # tiles is seen once per tile: this is a diagnostic counter, not an invariant.
        "candidate_pairs": collector.total,
        "objects_scanned": corpus_size,
        "objects_compared": probed,
        "mode": mode,
        "pending": pending,
        "suppressed": collector.suppressed,
        "incomplete": not complete,
        "elapsed_sec": round(time.monotonic() - started, 3),
    }
