"""The proposer must not restart itself on a backlog it never clears.

Two independent defects met here. A mission interrupted DURING planning left a
committed row with no tasks: `_pick_runnable` has nothing to run and `_finalize`
needs a non-empty task list, so nothing moved it to a terminal status — and since
`proposed` counts as non-terminal, the dedupe in `maybe_propose_from_backlog`
blocked every future proposal for good. With `operator_full_autonomy` on it also
held one of the eight active slots.

The mirror image: when missions DID complete, nothing throttled the next one. A
worker mission reads the Inbox, it does not clear it, so the ">= 10 pending"
condition was still true on the next cognition tick — and each mission files its
own produce step back into that same Inbox, so the queue grew by one per cycle and
the threshold could never fall on its own.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from jericho.executive.service import _PROPOSE_INBOX_THRESHOLD, ExecutiveService
from jericho.storage.models import MissionOrigin, MissionStatus


class _HangingPlanner:
    async def plan(self, goal: str):
        await asyncio.sleep(3600)
        raise AssertionError("unreachable")


class _TrivialPlanner:
    async def plan(self, goal: str):
        from jericho.executive.planner import PlannedTask

        return "план", [PlannedTask(seq=1, instruction="сводка", kind="produce", title="Сводка")]


def _service(settings, storage, planner) -> ExecutiveService:
    service = ExecutiveService.__new__(ExecutiveService)
    service.settings = settings
    service.storage = storage
    service.planner = planner
    service._audit = lambda *args, **kwargs: None  # noqa: SLF001
    return service


@pytest.mark.asyncio
async def test_a_mission_interrupted_while_planning_does_not_linger(settings, storage):
    storage.ensure_user("alice")
    service = _service(replace(settings, autonomy_enabled=True), storage, _HangingPlanner())

    with pytest.raises((TimeoutError, asyncio.CancelledError)):
        async with asyncio.timeout(0.05):
            await service.create_mission(
                "alice", "Разобрать входящие", origin=MissionOrigin.WORKER, created_by="worker"
            )

    missions = storage.list_missions("alice", limit=10)
    assert missions, "the mission row was never written, so there is nothing to check"
    assert missions[0]["status"] == MissionStatus.FAILED.value, (
        "a mission with no tasks stayed non-terminal and blocked the proposer forever"
    )


@pytest.mark.asyncio
async def test_the_proposer_waits_after_its_last_mission(settings, storage, monkeypatch):
    storage.ensure_user("alice")
    service = _service(replace(settings, autonomy_enabled=True), storage, _TrivialPlanner())

    detailed = [
        {"id": f"inb_{index}", "source_ref": f"telegram:{index}"}
        for index in range(_PROPOSE_INBOX_THRESHOLD + 2)
    ]
    monkeypatch.setattr(storage, "list_inbox_detailed", lambda *a, **k: detailed)

    first = await service.maybe_propose_from_backlog("alice")
    assert first is not None, "a real backlog should still get one mission"

    mission_id = str(first.get("id") or "")
    storage.update_mission_fields(mission_id, "alice", status=MissionStatus.COMPLETED.value)

    again = await service.maybe_propose_from_backlog("alice")
    assert again is None, "the proposer restarted itself on the same untouched backlog"


@pytest.mark.asyncio
async def test_the_proposers_own_output_is_not_the_backlog(settings, storage, monkeypatch):
    """Ten items, all of them this mechanism's own summaries, is a backlog of zero."""
    storage.ensure_user("alice")
    service = _service(replace(settings, autonomy_enabled=True), storage, _TrivialPlanner())

    own = [
        {"id": f"inb_{index}", "source_ref": f"mission:msn_{index}:task:1"}
        for index in range(_PROPOSE_INBOX_THRESHOLD + 5)
    ]
    monkeypatch.setattr(storage, "list_inbox_detailed", lambda *a, **k: own)

    assert await service.maybe_propose_from_backlog("alice") is None
