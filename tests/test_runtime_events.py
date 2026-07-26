"""The operational journal: what happened while nobody was watching.

``runtime_events`` was declared in the schema and carried by every migration with zero
readers and zero writers. The choice was to delete it or to use it, and using it won
for a concrete reason: every operational question in this system currently ends in
grepping raw logs and correlating timestamps by hand. A tunnel outage was diagnosed
that way — 295 failures across three days, counted from a log file.

Two properties matter more than the contents:

* **It is bounded.** An unbounded journal is a worse defect than the empty table it
  replaces, because it fails later and on a full disk.
* **It records transitions, not states.** Current health already lives in
  ``runtime_kv``. What is missing is "did anything break overnight and did it recover",
  and answering that per tick would write hundreds of rows to say one thing.
"""

from __future__ import annotations

import json

from jericho.storage._base import RUNTIME_EVENT_CAP


def test_events_round_trip_with_their_payload(storage):
    storage.record_event("backup.created", {"database": "jericho-x.sqlite3", "size_bytes": 4096})

    events = storage.list_events()
    assert len(events) == 1
    assert events[0]["event_type"] == "backup.created"
    assert events[0]["payload"]["database"] == "jericho-x.sqlite3"
    assert events[0]["payload"]["size_bytes"] == 4096


def test_the_journal_is_capped(storage):
    """A burst must cost bounded disk, not eventual disk."""
    for index in range(RUNTIME_EVENT_CAP + 50):
        storage.record_event("worker.failed", {"worker": f"w{index}"})

    assert storage.count_events() == RUNTIME_EVENT_CAP
    # The newest survive: a journal that drops the most recent event is useless.
    newest = storage.list_events(limit=1)[0]
    assert newest["payload"]["worker"] == f"w{RUNTIME_EVENT_CAP + 49}"


def test_events_can_be_filtered_by_type(storage):
    storage.record_event("worker.failed", {"worker": "embeddings_index"})
    storage.record_event("backup.created", {"database": "x"})
    storage.record_event("worker.recovered", {"worker": "embeddings_index"})

    assert len(storage.list_events()) == 3
    assert len(storage.list_events(event_type="worker.failed")) == 1
    assert [e["event_type"] for e in storage.list_events(event_type="backup.created")] == ["backup.created"]


# --- worker transitions ---------------------------------------------------


class _Manager:
    """The two attributes _persist_worker_state actually touches."""

    from jericho.workers import WorkersManager

    _FAILED_STATUSES = WorkersManager._FAILED_STATUSES
    _persist_worker_state = WorkersManager._persist_worker_state
    _record_worker_transition = WorkersManager._record_worker_transition

    def __init__(self, storage):
        self.storage = storage
        self._worker_failing: dict[str, bool] = {}


def _types(storage) -> list[str]:
    return [event["event_type"] for event in reversed(storage.list_events(limit=100))]


def test_a_worker_failing_and_recovering_is_two_events_not_two_hundred(storage):
    manager = _Manager(storage)

    manager._persist_worker_state("dedup", {"status": "ok"})
    for _ in range(100):  # broken all night on a short interval
        manager._persist_worker_state("dedup", {"status": "error", "error_type": "TimeoutError"})
    for _ in range(100):
        manager._persist_worker_state("dedup", {"status": "ok"})

    assert _types(storage) == ["worker.failed", "worker.recovered"]


def test_the_first_successful_run_after_start_is_not_a_recovery(storage):
    """Nothing broke; a fresh process must not report that something healed."""
    manager = _Manager(storage)
    manager._persist_worker_state("dedup", {"status": "ok"})
    assert storage.count_events() == 0


def test_running_heartbeats_are_not_events(storage):
    manager = _Manager(storage)
    for _ in range(10):
        manager._persist_worker_state("dedup", {"status": "running"})
    assert storage.count_events() == 0


def test_the_failure_event_carries_what_a_reader_needs(storage):
    manager = _Manager(storage)
    manager._persist_worker_state("dedup", {"status": "ok"})
    manager._persist_worker_state(
        "dedup", {"status": "timeout", "error_type": "TimeoutError", "consecutive_failures": 3}
    )

    event = storage.list_events(event_type="worker.failed")[0]
    assert event["payload"] == {
        "worker": "dedup",
        "status": "timeout",
        "error_type": "TimeoutError",
        "consecutive_failures": 3,
    }


def test_journalling_never_takes_down_the_worker_it_observes(storage, monkeypatch):
    """Observability that can crash the thing it observes is a liability."""
    manager = _Manager(storage)

    def explode(*_args, **_kwargs):
        raise RuntimeError("disk full")

    manager._persist_worker_state("dedup", {"status": "ok"})
    monkeypatch.setattr(storage, "record_event", explode)
    manager._persist_worker_state("dedup", {"status": "error", "error_type": "ValueError"})

    # The health snapshot still landed, which is what the status view reads.
    stored = json.loads(storage.kv_get("workers:health:dedup"))
    assert stored["status"] == "error"
