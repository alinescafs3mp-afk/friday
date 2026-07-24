from __future__ import annotations

import asyncio

import pytest

from jericho.workers import IntervalTask, WorkerBatchError, WorkersManager, WorkerSupervisor


@pytest.mark.asyncio
async def test_worker_supervisor_persists_timeout_without_crashing_scheduler():
    published: list[tuple[str, dict]] = []

    async def slow_task() -> None:
        await asyncio.sleep(1)

    supervisor = WorkerSupervisor(lambda name, state: published.append((name, dict(state))))
    task = IntervalTask(
        name="slow",
        func=slow_task,
        interval_sec=1.0,
        timeout_sec=0.02,
    )
    supervisor._running = True  # noqa: SLF001 - focused scheduler regression
    handle = asyncio.create_task(supervisor._run_task(task))  # noqa: SLF001
    supervisor._handles = [handle]  # noqa: SLF001
    try:
        for _ in range(100):
            if supervisor.snapshot().get("slow", {}).get("status") == "timeout":
                break
            await asyncio.sleep(0.005)
        state = supervisor.snapshot()["slow"]
        assert state["status"] == "timeout"
        assert state["consecutive_failures"] == 1
        assert state["error_type"] == "TimeoutError"
        assert any(item[1].get("status") == "running" for item in published)
        assert any(item[1].get("status") == "timeout" for item in published)
    finally:
        await supervisor.stop()


@pytest.mark.asyncio
async def test_inbox_advice_isolates_items_but_reports_degraded_batch(settings):
    class FakeStorage:
        @staticmethod
        def list_inbox_detailed(user_id, status, limit):
            del user_id, status, limit
            return [{"id": "bad"}, {"id": "good"}]

    class FakeIngestion:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def advise_inbox_item(self, user_id, item_id, **kwargs):
            del user_id, kwargs
            self.calls.append(item_id)
            if item_id == "bad":
                raise ValueError("malformed item")
            return {"idempotent_replay": False}

    class FakeLLM:
        enabled = True

    ingestion = FakeIngestion()
    manager = WorkersManager(
        settings,
        FakeStorage(),
        ingestion,
        kg=object(),
        llm=FakeLLM(),
    )

    with pytest.raises(WorkerBatchError, match="1 Inbox advice item"):
        await manager._inbox_model_advice("alice")  # noqa: SLF001

    assert ingestion.calls == ["bad", "good"]
