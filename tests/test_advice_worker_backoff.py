"""A background worker must not spend its whole budget on an endpoint that is down.

Observed on this instance: the model endpoint went offline while 66 imported items sat
pending. ``inbox_model_advice`` walks up to 50 of them and stops after **two successes**
— so with no successes possible it walked all fifty, each one running three LLM retries
with backoff inside, and hit its eight-minute timeout. The log recorded 46 identical
failures and the journal one ``worker.failed``.

Every part of that was wasted: the batch could not succeed, the budget was gone for the
cycle, and a struggling endpoint got fifty more rounds of traffic.

Isolated failures are different and must still be stepped over — one malformed document
should not stop the other forty-nine. The distinction is consecutiveness.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from jericho.storage.models import RawObject, new_id
from jericho.workers import _ADVICE_ENDPOINT_DOWN_AFTER, WorkerBatchError, WorkersManager


def _pending(storage, index: int) -> str:
    raw = storage.store_raw_object(
        RawObject(
            id=new_id("raw"),
            user_id="alice",
            source="upload",
            source_ref=f"sha256:{index:064d}",
            raw_content=f"материал {index}",
            content_type="text/plain",
            content_hash=f"{index:064d}",
            received_at=datetime.now(UTC).isoformat(),
        )
    )
    inbox_id = new_id("inbox")
    storage.execute(
        "INSERT INTO inbox (id, user_id, raw_object_id, status, suggested_action, "
        "promotion_score, quality_score, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (inbox_id, "alice", raw.id, "pending", "review", 0.9, 0.9, datetime.now(UTC).isoformat()),
    )
    storage.conn.commit()
    return inbox_id


class _Ingestion:
    """Counts advice attempts and fails them on demand."""

    def __init__(self, *, fail: set[int] | None = None, fail_all: bool = False):
        self.attempts = 0
        self._fail = fail or set()
        self._fail_all = fail_all

    async def advise_inbox_item(self, *_args, **_kwargs):
        self.attempts += 1
        if self._fail_all or self.attempts in self._fail:
            raise RuntimeError("LLM transport error")
        return {"advice": "ok"}


def _manager(settings, storage, ingestion) -> WorkersManager:
    from jericho.knowledge_graph import KnowledgeGraph

    class _LLM:
        enabled = True

    return WorkersManager(settings, storage, ingestion, KnowledgeGraph(storage), llm=_LLM())


@pytest.fixture
def seeded(storage):
    storage.ensure_user("alice", source="upload")
    for index in range(50):
        _pending(storage, index)
    return storage


def test_a_dead_endpoint_stops_the_batch_instead_of_burning_it(settings, seeded):
    """Fifty items, none of which can succeed. It must give up after a few."""
    ingestion = _Ingestion(fail_all=True)
    manager = _manager(settings, seeded, ingestion)

    with pytest.raises(WorkerBatchError):
        asyncio.run(manager._inbox_model_advice("alice"))

    assert ingestion.attempts == _ADVICE_ENDPOINT_DOWN_AFTER, (
        f"walked {ingestion.attempts} items against a dead endpoint"
    )


def test_an_isolated_failure_does_not_stop_the_batch(settings, seeded):
    """One bad document must not cost the other forty-nine their turn."""
    ingestion = _Ingestion(fail={1})
    manager = _manager(settings, seeded, ingestion)

    with pytest.raises(WorkerBatchError):
        asyncio.run(manager._inbox_model_advice("alice"))

    # Attempt 1 failed, 2 and 3 succeeded — the batch stops at two successes, as before.
    assert ingestion.attempts == 3


def test_failures_separated_by_a_success_are_not_treated_as_an_outage(settings, seeded):
    """Consecutiveness is the signal; a success resets it."""
    ingestion = _Ingestion(fail={1, 3, 5})
    manager = _manager(settings, seeded, ingestion)

    with pytest.raises(WorkerBatchError):
        asyncio.run(manager._inbox_model_advice("alice"))

    # 1 fails, 2 succeeds (reset), 3 fails, 4 succeeds — two successes, batch done.
    assert ingestion.attempts == 4


def test_a_healthy_endpoint_still_stops_after_two_successes(settings, seeded):
    """The existing bound on work per cycle is unchanged."""
    ingestion = _Ingestion()
    manager = _manager(settings, seeded, ingestion)

    asyncio.run(manager._inbox_model_advice("alice"))

    assert ingestion.attempts == 2


def test_the_failure_is_still_reported_to_the_supervisor(settings, seeded):
    """Giving up early must not look like success — the journal records the transition."""
    ingestion = _Ingestion(fail_all=True)
    manager = _manager(settings, seeded, ingestion)

    with pytest.raises(WorkerBatchError, match="failed"):
        asyncio.run(manager._inbox_model_advice("alice"))
