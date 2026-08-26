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
async def test_graph_expiry_worker_uses_checked_graph_adapter_with_one_bounded_page(
    settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[object] = []
    storage = object()

    class Adapter:
        def __init__(self, current_storage: object) -> None:
            events.append(("adapter", current_storage))

        def expire_due(self, **kwargs: object) -> object:
            events.append(("expire", kwargs))
            return type("Batch", (), {"retired": (object(), object()), "retained": ()})()

    monkeypatch.setattr(
        "friday.orchestration.supervisor_assist_graph_adapter.SupervisorAssistGraphAdapter",
        Adapter,
    )
    manager = WorkersManager(settings, storage, ingestion=None, kg=None)

    await manager._semantic_supervisor_graph_expiry()  # noqa: SLF001

    assert events[0] == ("adapter", storage)
    assert events[1][0] == "expire"
    kwargs = events[1][1]
    assert isinstance(kwargs, dict)
    assert callable(kwargs["lifecycle_check"])
    assert kwargs["limit"] == 100
