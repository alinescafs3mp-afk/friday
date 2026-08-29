"""Linearization tests for mission settlement and final publication."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from friday.execution_kernel import POSTCONDITIONS, ExecutionKernel
from friday.executive.service import ExecutiveService
from friday.ingestion import IngestionPipeline
from friday.knowledge_graph import KnowledgeGraph
from friday.permissions import AuthorizationService
from friday.storage.models import Mission, MissionStatus, MissionTask, TaskKind, TaskStatus, new_id


def _service(settings, storage) -> ExecutiveService:
    authorization = AuthorizationService(storage)
    ingestion = IngestionPipeline(settings, storage, KnowledgeGraph(storage))
    kernel = ExecutionKernel(authorization, settings)
    return ExecutiveService(settings, storage, authorization, kernel, None, ingestion)


def _mission_with_task(storage, kind: TaskKind) -> tuple[dict, dict]:
    user_id = "alice"
    storage.ensure_user(user_id)
    mission = Mission(
        id=new_id("mis"),
        user_id=user_id,
        goal="проверить границу завершения",
        status=MissionStatus.READY,
        created_by=user_id,
        deadline_at=(datetime.now(UTC) + timedelta(hours=1)).isoformat(),
    )
    storage.create_mission(mission)
    task = MissionTask(
        id=new_id("mst"),
        mission_id=mission.id,
        user_id=user_id,
        seq=1,
        kind=kind,
        instruction="один шаг",
    )
    storage.set_mission_plan(
        mission.id,
        user_id,
        [task],
        plan_summary="один шаг",
        status=MissionStatus.READY,
    )
    return storage.get_mission(mission.id, user_id), storage.get_mission_tasks(mission.id, user_id)[0]


@pytest.mark.asyncio
async def test_expiry_before_publication_admission_blocks_inbox_effect(
    settings,
    storage,
    monkeypatch,
) -> None:
    mission, task = _mission_with_task(storage, TaskKind.PRODUCE)
    service = _service(settings, storage)
    route_calls = 0

    async def execute(*_args, **_kwargs):
        with storage.transaction() as conn:
            conn.execute(
                "UPDATE missions SET deadline_at=? WHERE id=?",
                ("2020-01-01T00:00:00+00:00", mission["id"]),
            )
        return "готовый результат", []

    async def route(*_args, **_kwargs):
        nonlocal route_calls
        route_calls += 1
        return "inbox-never"

    monkeypatch.setattr(service, "_execute_task", execute)
    monkeypatch.setattr(service, "_route_to_inbox", route)

    await service._run_task(mission, task, {1: task})  # noqa: SLF001

    recovered = storage.get_mission_tasks(mission["id"], mission["user_id"])[0]
    parent = storage.get_mission(mission["id"], mission["user_id"])
    assert route_calls == 0
    assert recovered["status"] == TaskStatus.PENDING.value
    assert "истёк срок" in recovered["error"]
    assert parent["status"] == MissionStatus.BLOCKED.value


@pytest.mark.asyncio
async def test_cancel_winning_publication_admission_race_blocks_inbox_effect(
    settings,
    storage,
    monkeypatch,
) -> None:
    mission, task = _mission_with_task(storage, TaskKind.PRODUCE)
    service = _service(settings, storage)
    route_calls = 0
    cancelled = False
    original_admit = service._admit_task_result  # noqa: SLF001

    async def execute(*_args, **_kwargs):
        return "готовый результат", []

    async def route(*_args, **_kwargs):
        nonlocal route_calls
        route_calls += 1
        return "inbox-never"

    def cancel_before_admission(*args, **kwargs):
        nonlocal cancelled
        if not cancelled:
            cancelled = storage.cancel_mission_and_tasks(mission["id"], mission["user_id"])
        return original_admit(*args, **kwargs)

    monkeypatch.setattr(service, "_execute_task", execute)
    monkeypatch.setattr(service, "_route_to_inbox", route)
    monkeypatch.setattr(service, "_admit_task_result", cancel_before_admission)

    await service._run_task(mission, task, {1: task})  # noqa: SLF001

    recovered = storage.get_mission_tasks(mission["id"], mission["user_id"])[0]
    assert cancelled
    assert route_calls == 0
    assert recovered["status"] == TaskStatus.SKIPPED.value


@pytest.mark.asyncio
async def test_cancel_after_pure_publication_admission_is_not_recorded_as_skipped(
    settings,
    storage,
    monkeypatch,
) -> None:
    mission, task = _mission_with_task(storage, TaskKind.PRODUCE)
    service = _service(settings, storage)
    route_calls = 0

    async def execute(*_args, **_kwargs):
        return "готовый результат", []

    async def route(*_args, **_kwargs):
        nonlocal route_calls
        route_calls += 1
        assert storage.cancel_mission_and_tasks(mission["id"], mission["user_id"])
        return "inbox-admitted"

    monkeypatch.setattr(service, "_execute_task", execute)
    monkeypatch.setattr(service, "_route_to_inbox", route)

    await service._run_task(mission, task, {1: task})  # noqa: SLF001

    recovered = storage.get_mission_tasks(mission["id"], mission["user_id"])[0]
    assert route_calls == 1
    assert recovered["status"] == TaskStatus.UNCERTAIN.value
    assert recovered["side_effect"] == 1


@pytest.mark.asyncio
async def test_cancel_after_publication_admission_preserves_prior_checkpoint(
    settings,
    storage,
    monkeypatch,
) -> None:
    mission, task = _mission_with_task(storage, TaskKind.PRODUCE)
    service = _service(settings, storage)
    route_calls = 0

    async def execute(*_args, **_kwargs):
        claimed = storage.get_mission_tasks(mission["id"], mission["user_id"])[0]
        assert storage.update_mission_task_fields(
            task["id"],
            mission["user_id"],
            expected_statuses=(TaskStatus.RUNNING.value,),
            expected_attempt=int(claimed["attempts"]),
            side_effect=1,
            checkpoint_json='{"tool":"prior_effect","arguments":{}}',
        )
        return "готовый результат", []

    async def route(*_args, **_kwargs):
        nonlocal route_calls
        route_calls += 1
        assert storage.cancel_mission_and_tasks(mission["id"], mission["user_id"])
        return "inbox-admitted"

    monkeypatch.setattr(service, "_execute_task", execute)
    monkeypatch.setattr(service, "_route_to_inbox", route)

    await service._run_task(mission, task, {1: task})  # noqa: SLF001

    recovered = storage.get_mission_tasks(mission["id"], mission["user_id"])[0]
    assert route_calls == 1
    assert recovered["status"] == TaskStatus.UNCERTAIN.value
    assert "prior_effect" in recovered["checkpoint_json"]


@pytest.mark.asyncio
async def test_expiry_before_pure_task_settlement_cannot_finish_done(
    settings,
    storage,
    monkeypatch,
) -> None:
    mission, task = _mission_with_task(storage, TaskKind.GATHER)
    service = _service(settings, storage)

    async def execute(*_args, **_kwargs):
        with storage.transaction() as conn:
            conn.execute(
                "UPDATE missions SET deadline_at=? WHERE id=?",
                ("2020-01-01T00:00:00+00:00", mission["id"]),
            )
        return "поздний результат", []

    monkeypatch.setattr(service, "_execute_task", execute)
    await service._run_task(mission, task, {1: task})  # noqa: SLF001

    recovered = storage.get_mission_tasks(mission["id"], mission["user_id"])[0]
    parent = storage.get_mission(mission["id"], mission["user_id"])
    assert recovered["status"] == TaskStatus.PENDING.value
    assert recovered["result"] == ""
    assert parent["status"] == MissionStatus.BLOCKED.value


@pytest.mark.parametrize("parent_state", ["cancelled", "expired", "malformed"])
def test_absent_effect_cannot_reopen_work_under_dead_parent(
    settings,
    storage,
    parent_state: str,
) -> None:
    mission, task = _mission_with_task(storage, TaskKind.GATHER)
    assert storage.claim_mission_task(
        task["id"],
        mission["user_id"],
        mission_id=mission["id"],
        expected_attempt=0,
    )
    assert storage.update_mission_task_fields(
        task["id"],
        mission["user_id"],
        expected_statuses=(TaskStatus.RUNNING.value,),
        expected_attempt=1,
        status=TaskStatus.UNCERTAIN.value,
        side_effect=1,
        checkpoint_json='{"tool":"s6_dead_parent_absent","arguments":{}}',
    )

    def verify(*_args):
        if parent_state == "cancelled":
            assert storage.cancel_mission_and_tasks(mission["id"], mission["user_id"])
        else:
            deadline = "not-a-deadline" if parent_state == "malformed" else "2020-01-01T00:00:00+00:00"
            with storage.transaction() as conn:
                conn.execute(
                    "UPDATE missions SET deadline_at=? WHERE id=?",
                    (deadline, mission["id"]),
                )
        return False, "absent"

    POSTCONDITIONS["s6_dead_parent_absent"] = verify
    try:
        assert _service(settings, storage)._reconcile_uncertain(mission) == 0  # noqa: SLF001
    finally:
        POSTCONDITIONS.pop("s6_dead_parent_absent", None)

    recovered = storage.get_mission_tasks(mission["id"], mission["user_id"])[0]
    assert recovered["status"] == TaskStatus.UNCERTAIN.value
    assert recovered["side_effect"] == 1
