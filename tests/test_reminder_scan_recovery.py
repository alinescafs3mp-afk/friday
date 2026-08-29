"""Bounded recovery checks for persisted worker clocks and reminder scans."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from friday.knowledge_graph import KnowledgeGraph
from friday.organs import ServiceContext
from friday.organs.reminders import scan_reminders
from friday.reminder_schedule import reminder_clock_description
from friday.storage._graph import _bounded_visible_reminder_event_page
from friday.storage.models import EntityType
from friday.workers import IntervalTask, WorkerSupervisor


def _interval_task(interval_sec: float = 86_400.0) -> IntervalTask:
    async def _noop() -> None: ...

    return IntervalTask(
        name="reminders_scan",
        func=_noop,
        interval_sec=interval_sec,
        run_immediately=False,
    )


def _private_reminder(
    graph: KnowledgeGraph,
    person_id: str,
    *,
    name: str,
    day: str,
    clock: str = "",
) -> str:
    event = graph.create_entity(
        person_id,
        name,
        EntityType.EVENT,
        description=reminder_clock_description(clock),
        deduplicate=False,
    )
    graph.set_event_time(
        person_id,
        str(event["id"]),
        day,
        source=f"reminder:{person_id}",
    )
    return str(event["id"])


def test_a_future_persisted_worker_timestamp_delays_at_most_one_interval() -> None:
    """A wall-clock rollback cannot postpone a periodic task for weeks."""

    task = _interval_task()
    supervisor = WorkerSupervisor()
    future = (datetime.now(UTC) + timedelta(days=30)).isoformat(timespec="seconds")
    supervisor.restore({task.name: {"last_finished_at": future}})

    delay = supervisor._initial_delay(task)  # noqa: SLF001

    assert delay == task.interval_sec


@pytest.mark.asyncio
async def test_due_reminder_after_more_than_one_page_of_future_decoys_is_enqueued(
    settings,
    storage,
    monkeypatch,
) -> None:
    """The first 200 visible rows cannot starve a due row at the tail."""

    import friday.organs.reminders as reminders_module

    fixed_now = datetime(2026, 8, 29, 12, 0, tzinfo=UTC)
    tuned = replace(
        settings,
        local_timezone="UTC",
        reminders_enabled=True,
        reminders_lead_days=1,
        quiet_hours_start=0,
        quiet_hours_end=0,
    )
    person_id = "telegram:test:5001"
    storage.ensure_user(person_id, source="telegram", metadata={"chat_id": "5001"})
    graph = KnowledgeGraph(storage)
    day = fixed_now.date().isoformat()

    # All decoys sort before the due tail and are visible, but their exact clock
    # is still in the future.  The historical one-page scan stopped among them.
    for index in range(205):
        _private_reminder(
            graph,
            person_id,
            name=f"A future decoy {index:03d}",
            day=day,
            clock="23:59",
        )
    tail_id = _private_reminder(
        graph,
        person_id,
        name="Z due tail",
        day=day,
    )

    monkeypatch.setattr(reminders_module, "local_now", lambda _settings: fixed_now)
    await scan_reminders(ServiceContext(settings=tuned, storage=storage, kg=graph, ingestion=None))

    pending = storage.list_pending_notifications(limit=100)
    assert [str(row["dedup_key"]) for row in pending] == [f"reminder:{tail_id}:{day}"]


def test_reminder_pages_preserve_shared_and_exact_person_admission(storage) -> None:
    """Paging must not widen the established private-owner predicate."""

    for user_id in ("shared", "alice", "bob"):
        storage.ensure_user(user_id)
    graph = KnowledgeGraph(storage)
    day = "2026-08-29"

    public = graph.create_entity(
        "shared",
        "A shared event",
        EntityType.EVENT,
        deduplicate=False,
    )
    graph.set_event_time("shared", str(public["id"]), day, source="document")
    mine = _private_reminder(graph, "alice", name="B mine", day=day)
    _private_reminder(graph, "bob", name="C foreign", day=day)

    rows, cursor, has_more = _bounded_visible_reminder_event_page(
        storage,
        "shared",
        "alice",
        start=day,
        end=day,
        limit=200,
    )

    assert [str(row["entity_id"]) for row in rows] == [str(public["id"]), mine]
    assert cursor is not None
    assert has_more is False
