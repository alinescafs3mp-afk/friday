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


def test_a_failed_step_does_not_hide_behind_a_successful_one(settings, storage):
    """«Хоть один шаг сделан» — не успех миссии.

    Типичная миссия — два шага: собрать материал и произвести результат. Провал
    ВТОРОГО означает, что человек не получил ничего, а первый лишь сходил в
    поиск. Правило `COMPLETED if done` отчитывалось «завершена» ровно в этом
    случае — то есть система сообщала об успехе там, где его не было.

    Спека v3 §5: успешный вызов инструмента не доказывает успех задачи. Минимум,
    который из этого следует: провалившийся шаг не бывает частью «завершено».

    Мутация: вернуть `status = COMPLETED if done else FAILED` — тест обязан
    покраснеть.
    """
    storage.ensure_user("alice")
    mission = Mission(
        id=new_id("mis"),
        user_id="alice",
        goal="Собрать и произвести",
        status=MissionStatus.RUNNING,
    )
    storage.create_mission(mission)
    plan = [
        MissionTask(
            id=new_id("mst"),
            mission_id=mission.id,
            user_id="alice",
            seq=1,
            kind=TaskKind.GATHER,
            instruction="Собрать материал",
        ),
        MissionTask(
            id=new_id("mst"),
            mission_id=mission.id,
            user_id="alice",
            seq=2,
            kind=TaskKind.PRODUCE,
            instruction="Произвести результат",
            depends_on_json=[1],
        ),
    ]
    storage.set_mission_plan(
        mission.id, "alice", plan, plan_summary="сбор и результат", status=MissionStatus.RUNNING
    )
    storage.update_mission_task_fields(plan[0].id, "alice", status=TaskStatus.DONE.value)
    storage.update_mission_task_fields(
        plan[1].id, "alice", status=TaskStatus.FAILED.value, error="не удалось"
    )

    service = _service(settings, storage)
    service._finalize(mission.id, "alice", storage.get_mission_tasks(mission.id, "alice"))  # noqa: SLF001

    finished = storage.get_mission(mission.id, "alice")
    assert finished["status"] == MissionStatus.FAILED.value, (
        "миссия отчиталась «завершена», хотя результат не произведён"
    )
    assert finished["completed_at"], "миссия обязана быть терминальной в любом случае"


@pytest.mark.asyncio
async def test_a_mission_step_cannot_call_a_tool_outside_its_allowed_set(settings, storage):
    """Список инструментов, отданный модели, — подсказка, а не ограничение.

    Модель вольна назвать любое имя, и до проверки оно исполнялось: шаг миссии
    наследует способности ВЛАДЕЛЬЦА, поэтому `memory_save`/`entity_create` прошли
    бы гейт ядра — и миссия писала бы в канон мимо единственного предусмотренного
    выхода (Inbox, один раз, руками исполнителя).

    Мутация: убрать проверку `call.name not in GATHER_TOOLS` — тест обязан
    покраснеть.
    """
    import json as _json

    from jericho.executive.service import GATHER_TOOLS

    storage.ensure_user("alice")
    service = _service(settings, storage)
    executed: list[str] = []

    class _ToolHappyLLM:
        enabled = True
        model = "test-model"

        def __init__(self) -> None:
            self.calls = 0

        async def chat(self, messages, **kwargs):
            del messages, kwargs
            self.calls += 1
            if self.calls == 1:
                return {
                    "content": _json.dumps(
                        {"tool": "memory_save", "arguments": {"content": "канон мимо ревью"}},
                        ensure_ascii=False,
                    ),
                    "finish_reason": "stop",
                }
            return {"content": "Готово: сведения собраны.", "finish_reason": "stop"}

    async def _spy_execute(name, arguments, *, actor):
        del arguments, actor
        executed.append(name)

        class _Result:
            def to_llm_message(self) -> str:
                return "ok"

        return _Result()

    service.llm = _ToolHappyLLM()
    service.kernel.execute = _spy_execute  # type: ignore[method-assign]
    service.kernel.get_tool_definitions = lambda actor: [  # type: ignore[method-assign]
        {"function": {"name": name}} for name in sorted(GATHER_TOOLS)
    ]

    actor = service.auth_service.actor_for_user("alice", source="test")
    await service._run_tool_loop("собери сведения", actor)  # noqa: SLF001

    assert "memory_save" not in executed, "шаг миссии исполнил инструмент вне разрешённого набора"
