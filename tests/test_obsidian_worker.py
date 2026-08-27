from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from friday.organs import ServiceContext
from friday.organs.obsidian import (
    ObsidianOrgan,
    ObsidianReconcileError,
    reconcile_obsidian,
)
from friday.workers import IntervalTask, WorkerSupervisor


class _AlternatingRuntime:
    def __init__(self, reports: list[dict[str, int]]) -> None:
        self._reports = iter(reports)

    async def reconcile(self) -> dict[str, int]:
        return next(self._reports)


def _context(runtime: object) -> ServiceContext:
    return ServiceContext(
        settings=SimpleNamespace(
            obsidian_enabled=True,
            obsidian_reconcile_interval_sec=10.0,
        ),
        storage=object(),
        kg=object(),
        ingestion=object(),
        obsidian=runtime,
    )


def test_obsidian_worker_bootstraps_immediately() -> None:
    worker = ObsidianOrgan().workers(_context(_AlternatingRuntime([])))[0]

    assert worker.enabled is True
    assert worker.run_immediately is True


@pytest.mark.asyncio
async def test_failed_profile_marks_worker_error_and_next_clean_sweep_recovers() -> None:
    runtime = _AlternatingRuntime(
        [
            {"checked": 0, "failed": 1},
            {"checked": 1, "failed": 0},
        ]
    )
    published: list[dict[str, object]] = []
    supervisor = WorkerSupervisor(lambda _name, state: published.append(dict(state)))
    task = IntervalTask(
        name="obsidian_reconcile",
        func=lambda: reconcile_obsidian(_context(runtime)),
        interval_sec=1.0,
        run_immediately=True,
        timeout_sec=5.0,
    )
    supervisor._running = True  # noqa: SLF001 - focused scheduler regression
    handle = asyncio.create_task(supervisor._run_task(task))  # noqa: SLF001
    supervisor._handles = [handle]  # noqa: SLF001
    try:
        for _ in range(300):
            state = supervisor.snapshot().get("obsidian_reconcile", {})
            if state.get("status") == "ok" and state.get("last_success_at"):
                break
            await asyncio.sleep(0.005)
        state = supervisor.snapshot()["obsidian_reconcile"]
        assert any(item.get("status") == "error" for item in published)
        assert any(
            item.get("status") == "error"
            and item.get("error_type") == "ObsidianReconcileError"
            and item.get("consecutive_failures") == 1
            for item in published
        )
        assert state["status"] == "ok"
        assert state["consecutive_failures"] == 0
        assert state["error_type"] is None
    finally:
        await supervisor.stop()


@pytest.mark.asyncio
async def test_failed_profile_report_is_not_a_false_success() -> None:
    with pytest.raises(ObsidianReconcileError, match="1 Obsidian profile"):
        await reconcile_obsidian(_context(_AlternatingRuntime([{"checked": 0, "failed": 1}])))
