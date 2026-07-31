"""Found by adversarial review ahead of a live multi-user demo (7 real Telegram
accounts able to search concurrently): `HybridSearcher._lexical_rank` ran
synchronously on the single shared event loop. Measured on the live corpus:
2.4 seconds of pure CPU for one cold-cache pass over 400 candidates — during
which every OTHER tenant's in-flight request is frozen, not merely slower.

The fix has two parts, and both are load-bearing:
1. `search()` offloads `_lexical_rank` via `run_blocking` (a thread), so the
   event loop stays free for other coroutines while it runs.
2. `_cached_vector`'s shared `_vector_cache` (an `OrderedDict`, not otherwise
   thread-safe) is now guarded by `_vector_cache_lock`, because (1) makes it
   reachable from more than one thread at once for the first time.

Fixing only (1) without (2) would trade a real, currently-present slowdown for
a race condition — worse, and easy to miss because races are timing-dependent.
Both are tested here independently.
"""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

import jericho.retrieval as retrieval_module
from jericho.retrieval import HybridSearcher


def _candidates(count: int) -> list[dict]:
    return [
        {
            "id": f"ko-{index}",
            "version": 1,
            "updated_at": "2026-08-01T00:00:00Z",
            "title": f"Документ номер {index}",
            "content": f"Содержимое документа {index}: договор поставки, отчёт по проекту, совещание.",
            "summary": "",
            "tags_json": "[]",
        }
        for index in range(count)
    ]


@pytest.mark.asyncio
async def test_lexical_rank_does_not_block_the_event_loop(settings, storage, monkeypatch):
    """A concurrently-scheduled coroutine must keep making progress WHILE a slow
    `_lexical_rank` pass runs — proof that `run_blocking` actually offloads it,
    not just that the offload call exists syntactically. `search()` calls
    `_lexical_rank` from TWO call sites (once before graph expansion, once
    after) — the assertion checks the largest gap between consecutive ticks,
    not a total tick count, so blocking at EITHER site is caught even if the
    other site's correct offload lets the ticker "catch up" afterwards and
    pass a naive sum-based threshold.

    Mutation: revert either `await run_blocking(self._lexical_rank, ...)`
    call site back to a direct `self._lexical_rank(...)` call — this test
    must go red (one gap around 0.3s instead of every gap staying small).
    """
    from jericho.storage.models import KnowledgeObject, RawObject, new_id

    storage.ensure_user("alice")
    for index in range(5):
        raw = RawObject(
            id=new_id("raw"),
            user_id="alice",
            source="test",
            source_ref=new_id("src"),
            raw_content=f"договор поставки номер {index}, отчёт по проекту",
            content_type="text",
        )
        storage.store_raw_object(raw)
        storage.store_knowledge_object(
            KnowledgeObject(
                id=new_id("ko"),
                user_id="alice",
                raw_object_id=raw.id,
                content=raw.raw_content,
                title=f"Документ {index}",
                summary=raw.raw_content,
            )
        )

    searcher = HybridSearcher(storage)

    real_lexical_rank = searcher._lexical_rank

    def _slow_lexical_rank(*args, **kwargs):
        time.sleep(0.3)
        return real_lexical_rank(*args, **kwargs)

    monkeypatch.setattr(searcher, "_lexical_rank", _slow_lexical_rank)

    tick_times: list[float] = []

    async def _ticker():
        while True:
            tick_times.append(time.monotonic())
            await asyncio.sleep(0.01)

    ticker_task = asyncio.create_task(_ticker())
    # `create_task` only SCHEDULES the ticker — it does not run until this
    # coroutine yields control, which does not happen just by writing `await
    # searcher.search(...)` if `search()`'s own first blocking work runs before
    # its own first internal suspend point. Without this explicit yield, a block
    # right at the start of `search()` would finish before the ticker ever got a
    # chance to record its first tick, leaving no gap to measure at all.
    await asyncio.sleep(0)
    try:
        await searcher.search("alice", "договор поставки", limit=5)
    finally:
        # Recorded BEFORE cancelling, and appended manually rather than relying on
        # one more real tick: a block that runs right up to `search()` returning
        # races `ticker_task.cancel()` against the ticker's already-overdue
        # `asyncio.sleep(0.01)` timer, and cancellation wins — the ticker's
        # `CancelledError` fires at that suspended await point without its loop
        # body ever running again, so no tick gets recorded for the tail end of
        # the block. Without this explicit marker, `tick_times` simply stops
        # before the gap it needed to reveal, and the gap is invisible to `max()`.
        tick_times.append(time.monotonic())
        ticker_task.cancel()

    assert len(tick_times) >= 5, f"only {len(tick_times)} ticks recorded — test setup is broken"
    gaps = [b - a for a, b in zip(tick_times, tick_times[1:], strict=False)]
    max_gap = max(gaps)
    assert max_gap < 0.15, (
        f"largest gap between ticker steps was {max_gap:.3f}s (expected ~0.01s) — "
        "the event loop was blocked for a stretch, not freed for other coroutines"
    )


def test_concurrent_cache_access_does_not_race(monkeypatch):
    """Stress the shared `_vector_cache` from many real OS threads at once, with
    a deliberately tiny cache ceiling so LRU eviction collides with concurrent
    reads on the same keys — the exact shape of the race `_vector_cache_lock`
    exists to prevent (a `.move_to_end()` on a key another thread just evicted
    via `.popitem()` raises `KeyError`).

    Mutation: remove the `with self._vector_cache_lock:` guards in
    `_cached_vector` (read the raw dict operations unguarded) — this test must
    go red, typically with a `KeyError` from a thread's `.move_to_end(key)`
    racing another thread's eviction of that same key.
    """

    class _FakeStorage:
        pass

    monkeypatch.setattr(retrieval_module, "_VECTOR_CACHE_MAX", 5)
    searcher = HybridSearcher(_FakeStorage())  # type: ignore[arg-type]
    candidates = _candidates(50)

    errors: list[BaseException] = []
    errors_lock = threading.Lock()

    def _worker():
        try:
            for _ in range(20):
                searcher._lexical_rank(candidates, "договор поставки отчёт")
        except BaseException as exc:  # noqa: BLE001 - capturing for the assertion below
            with errors_lock:
                errors.append(exc)

    threads = [threading.Thread(target=_worker) for _ in range(16)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert not errors, f"concurrent cache access raised: {errors!r}"
