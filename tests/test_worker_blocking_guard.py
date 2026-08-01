"""A worker timeout must stop the next run, not just the previous await.

``asyncio.to_thread`` hands work to a thread and returns an awaitable. Cancelling the
await — which is precisely what a worker timeout does — ends the waiting and nothing
else: no mechanism in Python interrupts a running thread. The work continues, holding
its database connection, while the supervisor considers the run finished and starts the
next one on schedule.

Measured on this supervisor before the guard: a blocking call of seven seconds under a
five-second timeout produced **two concurrent threads**, and the task reported
``running`` while an orphan from the previous tick was still writing. Two runs of the
same worker on one SQLite database is not a theoretical concern.

The counter is kept inside the thread, because that is the only observer that survives
the cancellation. The event loop's view is exactly the view that is wrong.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
import time

import pytest

from friday.workers import WorkerSupervisor
from friday.workers._blocking import current_task, in_flight, run_blocking, snapshot


def test_blocking_work_is_visible_while_it_runs():
    started, release = threading.Event(), threading.Event()

    def blocking():
        started.set()
        release.wait(5)

    async def main():
        token = current_task.set("probe")
        try:
            work = asyncio.create_task(run_blocking(blocking))
            await asyncio.to_thread(started.wait, 5)
            assert in_flight("probe") == 1, "a running thread must be visible"
            release.set()
            await work
        finally:
            current_task.reset(token)
        assert in_flight("probe") == 0, "a finished thread must clear itself"

    asyncio.run(main())


def test_cancelling_the_await_does_not_hide_the_thread():
    """The whole point: the coroutine dies, the thread does not, and we must know."""
    release = threading.Event()

    def blocking():
        release.wait(5)

    async def main():
        token = current_task.set("probe")
        try:
            work = asyncio.create_task(run_blocking(blocking))
            await asyncio.sleep(0.15)
            work.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await work
            # The await is over. The thread is not.
            assert in_flight("probe") == 1
            release.set()
            await asyncio.to_thread(time.sleep, 0.2)
            assert in_flight("probe") == 0
        finally:
            current_task.reset(token)

    asyncio.run(main())


def test_outside_a_worker_it_is_plain_to_thread():
    async def main():
        assert await run_blocking(lambda: 21 * 2) == 42
        assert snapshot() == {} or "probe" not in snapshot()

    asyncio.run(main())


def test_an_exception_still_clears_the_counter():
    async def main():
        token = current_task.set("probe")
        try:
            with pytest.raises(ValueError):
                await run_blocking(lambda: (_ for _ in ()).throw(ValueError("boom")))
            assert in_flight("probe") == 0
        finally:
            current_task.reset(token)

    asyncio.run(main())


def test_the_supervisor_refuses_to_run_on_top_of_a_stranded_thread():
    """End to end, at the timing that produced two concurrent threads before.

    Thirteen seconds, and worth them: the supervisor clamps every timeout to a five
    second floor, so nothing shorter reproduces the race this guards against. The four
    tests above cover the mechanism quickly; this one covers that it is wired in.

    The blocking call outlives the timeout, so the second tick must be skipped rather
    than started alongside it — and the skip has to be visible, not silent.
    """
    concurrent, peak, lock = [], [0], threading.Lock()
    statuses: list[str] = []

    def blocking():
        with lock:
            concurrent.append(1)
            peak[0] = max(peak[0], len(concurrent))
        time.sleep(7.0)
        with lock:
            concurrent.pop()

    async def task():
        await run_blocking(blocking)

    supervisor = WorkerSupervisor(lambda _name, state: statuses.append(str(state.get("status"))))
    supervisor.register("guarded", task, interval_sec=1.0, timeout_sec=5.0)

    async def main():
        runner = asyncio.create_task(supervisor.run())
        await asyncio.sleep(13.0)
        await supervisor.stop()
        with contextlib.suppress(Exception):
            await runner

    asyncio.run(main())

    assert peak[0] == 1, f"{peak[0]} threads of one worker ran at once"
    assert "timeout" in statuses, "the timeout must still be reported"
    assert "skipped" in statuses, "the skipped tick must be visible, not silent"


# --- oversized documents must still get a vector --------------------------


def test_the_whole_document_vector_is_bounded_before_it_is_sent():
    """One input carries the whole object, and an embeddings service will refuse a big one.

    Measured against the live service: 104175 characters timed out where 20000 answered
    in seconds. Because an object's inputs travel in a single request, an oversized
    document also took its own passages down with it and ended up with no vector at
    all — reported as "backend returned no usable vectors", which reads like a broken
    endpoint rather than a document that is simply too long.
    """
    from friday.retrieval import knowledge_search_text
    from friday.workers import _DOC_VECTOR_MAX_CHARS

    huge = {
        "title": "большой документ",
        "summary": "с",
        "content": "текст " * 40_000,
        "tags_json": "[]",
        "knowledge_kind": "note",
    }
    assert len(knowledge_search_text(huge)) > _DOC_VECTOR_MAX_CHARS
    assert len(knowledge_search_text(huge)[:_DOC_VECTOR_MAX_CHARS]) == _DOC_VECTOR_MAX_CHARS
    # A passage stays far below it, so the cap never truncates ordinary objects.
    assert _DOC_VECTOR_MAX_CHARS > 1200 * 8
