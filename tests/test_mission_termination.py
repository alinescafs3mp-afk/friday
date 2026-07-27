"""A mission must end, and a cancelled mission must stay cancelled.

Two ways a mission became immortal or un-stoppable, both found by audit and both
reproduced here through the service rather than through its parts.

* The skip cascade travelled exactly one level. A chain 1←2←3 whose first step
  failed left step 3 PENDING forever — not FAILED, not SKIPPED — and `_finalize`
  needs every task terminal, so the mission stayed RUNNING with no completion
  time, no audit row and nothing to notice it. A failing step is an ordinary
  path: the model being briefly unavailable raises `MissionStepUnavailable`.

* `cancel_mission` is two UPDATEs with nothing to interrupt a step already
  waiting on the model. When that step returned it wrote its result to Inbox
  AFTER the stop, flipped its own SKIPPED back to DONE, and `_finalize` then
  turned the mission's CANCELLED into COMPLETED. Stop did not stop.
"""

from __future__ import annotations

import pytest

from jericho.execution_kernel import ExecutionKernel
from jericho.executive.service import ExecutiveService
from jericho.ingestion import IngestionPipeline
from jericho.knowledge_graph import KnowledgeGraph
from jericho.permissions import AuthorizationService
from jericho.storage.models import Mission, MissionStatus, MissionTask, TaskKind, TaskStatus, new_id


def _service(settings, storage) -> ExecutiveService:
    auth = AuthorizationService(storage)
    graph = KnowledgeGraph(storage)
    ingestion = IngestionPipeline(settings, storage, graph)
    kernel = ExecutionKernel(auth, settings)
    return ExecutiveService(settings, storage, auth, kernel, None, ingestion)


def _mission_with_chain(storage, user_id: str = "alice") -> tuple[str, list[str]]:
    """Three steps where each depends on the one before it."""
    storage.ensure_user(user_id)
    mission = Mission(
        id=new_id("mis"),
        user_id=user_id,
        goal="Проверить цепочку шагов",
        status=MissionStatus.RUNNING,
    )
    storage.create_mission(mission)
    tasks = [
        MissionTask(
            id=new_id("mst"),
            mission_id=mission.id,
            user_id=user_id,
            seq=seq,
            kind=TaskKind.GATHER if seq < 3 else TaskKind.PRODUCE,
            instruction=f"Шаг {seq}",
            depends_on_json=[seq - 1] if seq > 1 else [],
        )
        for seq in (1, 2, 3)
    ]
    storage.set_mission_plan(mission.id, user_id, tasks, plan_summary="цепочка", status=MissionStatus.RUNNING)
    return mission.id, [task.id for task in tasks]


def test_a_skip_cascades_all_the_way_down(settings, storage):
    service = _service(settings, storage)
    mission_id, task_ids = _mission_with_chain(storage)

    # Step 1 fails the way an unavailable model makes it fail.
    storage.update_mission_task_fields(
        task_ids[0], "alice", status=TaskStatus.FAILED.value, error="model unavailable"
    )

    tasks = storage.get_mission_tasks(mission_id, "alice")
    by_seq = {int(task["seq"]): task for task in tasks}
    # Two passes: the first skips step 2, the second must then skip step 3.
    for _ in range(3):
        service._pick_runnable("alice", storage.get_mission_tasks(mission_id, "alice"), by_seq)  # noqa: SLF001
        tasks = storage.get_mission_tasks(mission_id, "alice")
        by_seq = {int(task["seq"]): task for task in tasks}

    statuses = {int(task["seq"]): task["status"] for task in tasks}
    assert statuses[2] == TaskStatus.SKIPPED.value
    assert statuses[3] == TaskStatus.SKIPPED.value, (
        f"step 3 stayed {statuses[3]}: the mission can never reach a terminal state"
    )

    service._finalize(mission_id, "alice", tasks)  # noqa: SLF001
    mission = storage.get_mission(mission_id, "alice")
    assert mission["status"] in {MissionStatus.FAILED.value, MissionStatus.COMPLETED.value}
    assert mission["completed_at"]


@pytest.mark.asyncio
async def test_cancelling_a_mission_is_not_undone_by_a_step_in_flight(settings, storage):
    service = _service(settings, storage)
    mission_id, task_ids = _mission_with_chain(storage)

    cancelled = await service.cancel_mission(mission_id, "alice")
    assert MissionStatus.CANCELLED.value in str(cancelled)

    # The step that was already waiting on the model comes back now.
    tasks = storage.get_mission_tasks(mission_id, "alice")
    service._finalize(mission_id, "alice", tasks)  # noqa: SLF001

    mission = storage.get_mission(mission_id, "alice")
    assert mission["status"] == MissionStatus.CANCELLED.value, (
        "the stop was overwritten by the tick that was already running"
    )
