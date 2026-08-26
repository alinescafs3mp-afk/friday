from __future__ import annotations

import pytest

from friday import workers
from friday.workers import WorkersManager


def test_graph_expiry_worker_is_unconditional_and_runs_immediately(settings) -> None:
    manager = WorkersManager(settings, storage=object(), ingestion=None, kg=None)

    manager.register_all()

    tasks = {task.name: task for task in manager.supervisor._tasks}  # noqa: SLF001
    expiry = tasks["semantic_supervisor_graph_expiry"]
    assert expiry.enabled is True
    assert expiry.run_immediately is True
    assert expiry.interval_sec == 60.0
    assert expiry.timeout_sec == 30.0


@pytest.mark.asyncio
async def test_graph_expiry_worker_uses_one_bounded_storage_transaction(
    settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[object] = []
    connection = object()

    class Transaction:
        def __enter__(self) -> object:
            events.append("begin")
            return connection

        def __exit__(self, exc_type, exc, traceback) -> None:
            del exc_type, exc, traceback
            events.append("commit")

    class Storage:
        @staticmethod
        def transaction() -> Transaction:
            return Transaction()

    def expire(conn: object, *, limit: int):
        events.append((conn, limit))
        return (object(), object())

    monkeypatch.setattr(
        workers,
        "expire_due_compare_current_file_web_work_graphs_in_transaction",
        expire,
    )
    manager = WorkersManager(settings, Storage(), ingestion=None, kg=None)

    await manager._semantic_supervisor_graph_expiry()  # noqa: SLF001

    assert events == ["begin", (connection, 100), "commit"]
