"""Worker health bookkeeping must not stall the product.

`_publish` runs on the event loop several times per task run, and its state sink
writes to SQLite. Every write takes the process-wide write lock, so while a
worker thread holds a long write transaction — one batch of embedding vectors is
committed in a single transaction — that inline `kv_set` blocked the loop for the
whole duration. Measured at 3.00 seconds against a 3-second writer: no HTTP
request, no Telegram message, no other worker, all so a heartbeat could be
recorded.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from jericho.workers import WorkerSupervisor


@pytest.mark.asyncio
async def test_a_slow_state_sink_does_not_block_the_event_loop():
    """The sink sleeps like a blocked write; the loop must keep ticking."""
    written: list[str] = []

    def slow_sink(name: str, state: dict) -> None:
        time.sleep(0.3)  # a write waiting on the storage lock  # noqa: ASYNC251
        written.append(f"{name}:{state.get('status')}")

    supervisor = WorkerSupervisor(state_sink=slow_sink)
    ticks = 0

    async def heartbeat() -> None:
        nonlocal ticks
        while True:
            await asyncio.sleep(0.01)
            ticks += 1

    async def once() -> None:
        return None

    supervisor.register("probe", once, interval_sec=0.05)
    beat = asyncio.create_task(heartbeat())
    runner = asyncio.create_task(supervisor.run())
    await asyncio.sleep(0.6)
    await supervisor.stop()
    runner.cancel()
    beat.cancel()

    # Three sink writes of 0.3 s each would have cost ~0.9 s of frozen loop; a
    # loop that kept ticking every 10 ms records dozens of beats in 0.6 s.
    assert ticks >= 20, f"the event loop ticked only {ticks} times"
    assert written, "the state was never persisted at all"


@pytest.mark.asyncio
async def test_shutdown_flushes_the_queued_health_writes():
    """The last thing a task publishes is why it stopped; losing it hides that."""
    written: list[tuple[str, str]] = []

    def sink(name: str, state: dict) -> None:
        written.append((name, str(state.get("status"))))

    supervisor = WorkerSupervisor(state_sink=sink)

    async def once() -> None:
        return None

    supervisor.register("probe", once, interval_sec=5.0)
    runner = asyncio.create_task(supervisor.run())
    await asyncio.sleep(0.2)
    await supervisor.stop()
    runner.cancel()

    statuses = [status for _, status in written]
    assert "ok" in statuses, statuses


def test_publish_without_a_running_loop_still_writes():
    """Unit tests call `_publish` directly; that path must keep working."""
    written: list[str] = []
    supervisor = WorkerSupervisor(state_sink=lambda name, state: written.append(name))

    async def once() -> None:
        return None

    supervisor.register("probe", once, interval_sec=5.0)
    supervisor._publish(supervisor._tasks[0], status="ok")  # noqa: SLF001
    assert written == ["probe"]
