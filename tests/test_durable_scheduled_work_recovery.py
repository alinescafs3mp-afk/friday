"""Adversarial recovery boundaries for durable reminders and missions."""

from __future__ import annotations

import asyncio
import threading
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


def _mission_with_task(storage, *, user_id: str = "alice") -> tuple[dict, dict]:
    storage.ensure_user(user_id)
    mission = Mission(
        id=new_id("mis"),
        user_id=user_id,
        goal="проверить восстановление",
        status=MissionStatus.READY,
        created_by=user_id,
    )
    storage.create_mission(mission)
    task = MissionTask(
        id=new_id("mst"),
        mission_id=mission.id,
        user_id=user_id,
        seq=1,
        kind=TaskKind.GATHER,
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


def test_two_workers_only_one_claims_pending_task(storage) -> None:
    mission, task = _mission_with_task(storage)
    barrier = threading.Barrier(2)
    outcomes: list[bool] = []
    failures: list[BaseException] = []
    result_lock = threading.Lock()

    def claimant() -> None:
        try:
            barrier.wait(timeout=5)
            outcome = storage.claim_mission_task(
                task["id"],
                mission["user_id"],
                mission_id=mission["id"],
                expected_attempt=int(task["attempts"]),
            )
            with result_lock:
                outcomes.append(outcome)
        except BaseException as exc:  # noqa: BLE001 - surface thread failures
            with result_lock:
                failures.append(exc)

    workers = [threading.Thread(target=claimant) for _ in range(2)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=10)

    assert not failures
    assert sorted(outcomes) == [False, True]
    row = storage.get_mission_tasks(mission["id"], mission["user_id"])[0]
    assert row["status"] == TaskStatus.RUNNING.value
    assert row["attempts"] == 1


def test_restart_claim_counts_retry_without_resetting_attempts(storage) -> None:
    mission, task = _mission_with_task(storage)
    assert storage.claim_mission_task(
        task["id"],
        mission["user_id"],
        mission_id=mission["id"],
        expected_attempt=int(task["attempts"]),
    )
    claimed = storage.get_mission_tasks(mission["id"], mission["user_id"])[0]
    with storage.transaction() as conn:
        conn.execute(
            "UPDATE mission_tasks SET status='pending', started_at='' WHERE id=?",
            (task["id"],),
        )

    assert storage.claim_mission_task(
        task["id"],
        mission["user_id"],
        mission_id=mission["id"],
        expected_attempt=int(claimed["attempts"]),
    )
    task_row = storage.get_mission_tasks(mission["id"], mission["user_id"])[0]
    mission_row = storage.get_mission(mission["id"], mission["user_id"])
    assert task_row["attempts"] == 2
    assert mission_row["spent_retries"] == 1


def test_stale_attempt_cannot_finish_a_new_execution_owner(storage) -> None:
    mission, task = _mission_with_task(storage)
    assert storage.claim_mission_task(
        task["id"],
        mission["user_id"],
        mission_id=mission["id"],
        expected_attempt=int(task["attempts"]),
    )
    first_attempt = storage.get_mission_tasks(mission["id"], mission["user_id"])[0]["attempts"]
    assert storage.update_mission_task_fields(
        task["id"],
        mission["user_id"],
        expected_statuses=(TaskStatus.RUNNING.value,),
        expected_attempt=first_attempt,
        status=TaskStatus.PENDING.value,
        started_at="",
    )
    assert storage.claim_mission_task(
        task["id"],
        mission["user_id"],
        mission_id=mission["id"],
        expected_attempt=int(first_attempt),
    )

    assert not storage.update_mission_task_fields(
        task["id"],
        mission["user_id"],
        expected_statuses=(TaskStatus.RUNNING.value,),
        expected_attempt=first_attempt,
        status=TaskStatus.DONE.value,
    )
    current = storage.get_mission_tasks(mission["id"], mission["user_id"])[0]
    assert current["status"] == TaskStatus.RUNNING.value
    assert current["attempts"] == first_attempt + 1


def test_stale_pending_snapshot_cannot_claim_after_status_aba(storage) -> None:
    mission, stale_pending = _mission_with_task(storage)
    assert storage.claim_mission_task(
        stale_pending["id"],
        mission["user_id"],
        mission_id=mission["id"],
        expected_attempt=int(stale_pending["attempts"]),
    )
    first_owner = storage.get_mission_tasks(mission["id"], mission["user_id"])[0]
    assert storage.update_mission_task_fields(
        first_owner["id"],
        mission["user_id"],
        expected_statuses=(TaskStatus.RUNNING.value,),
        expected_attempt=int(first_owner["attempts"]),
        status=TaskStatus.PENDING.value,
        started_at="",
    )

    # Status is PENDING again, but the old snapshot must not acquire a new
    # execution owner or spend a retry after this RUNNING -> PENDING ABA.
    assert not storage.claim_mission_task(
        stale_pending["id"],
        mission["user_id"],
        mission_id=mission["id"],
        expected_attempt=int(stale_pending["attempts"]),
    )
    reopened = storage.get_mission_tasks(mission["id"], mission["user_id"])[0]
    unchanged_mission = storage.get_mission(mission["id"], mission["user_id"])
    assert reopened["status"] == TaskStatus.PENDING.value
    assert reopened["attempts"] == first_owner["attempts"]
    assert unchanged_mission["spent_retries"] == 0

    assert storage.claim_mission_task(
        reopened["id"],
        mission["user_id"],
        mission_id=mission["id"],
        expected_attempt=int(reopened["attempts"]),
    )
    second_owner = storage.get_mission_tasks(mission["id"], mission["user_id"])[0]
    assert second_owner["status"] == TaskStatus.RUNNING.value
    assert second_owner["attempts"] == first_owner["attempts"] + 1
    assert storage.get_mission(mission["id"], mission["user_id"])["spent_retries"] == 1


def test_stale_reaper_cannot_clear_a_live_effect_checkpoint(storage) -> None:
    mission, task = _mission_with_task(storage)
    assert storage.claim_mission_task(
        task["id"],
        mission["user_id"],
        mission_id=mission["id"],
        expected_attempt=0,
    )
    claimed = storage.get_mission_tasks(mission["id"], mission["user_id"])[0]
    assert storage.update_mission_task_fields(
        task["id"],
        mission["user_id"],
        expected_statuses=(TaskStatus.RUNNING.value,),
        expected_attempt=int(claimed["attempts"]),
        side_effect=1,
        checkpoint_json='{"tool":"live_effect","arguments":{}}',
    )

    assert not storage.update_mission_task_fields(
        task["id"],
        mission["user_id"],
        expected_statuses=(TaskStatus.RUNNING.value,),
        expected_attempt=int(claimed["attempts"]),
        status=TaskStatus.PENDING.value,
        started_at="",
    )
    durable = storage.get_mission_tasks(mission["id"], mission["user_id"])[0]
    assert durable["status"] == TaskStatus.RUNNING.value
    assert durable["side_effect"] == 1
    assert "live_effect" in durable["checkpoint_json"]


@pytest.mark.parametrize("invalid_attempt", [-1, False, 0.9, "0"])
def test_execution_attempt_witness_is_not_coerced(storage, invalid_attempt) -> None:
    mission, task = _mission_with_task(storage)
    assert not storage.claim_mission_task(
        task["id"],
        mission["user_id"],
        mission_id=mission["id"],
        expected_attempt=invalid_attempt,
    )
    assert not storage.update_mission_task_fields(
        task["id"],
        mission["user_id"],
        expected_attempt=invalid_attempt,
        status=TaskStatus.RUNNING,
    )
    durable = storage.get_mission_tasks(mission["id"], mission["user_id"])[0]
    assert durable["status"] == TaskStatus.PENDING.value
    assert durable["attempts"] == 0


def test_enum_status_uses_the_same_closed_transition_fences(storage) -> None:
    mission, task = _mission_with_task(storage)
    assert storage.update_mission_fields(
        mission["id"],
        mission["user_id"],
        status=MissionStatus.CANCELLED,
    )
    assert not storage.update_mission_task_fields(
        task["id"],
        mission["user_id"],
        status=TaskStatus.RUNNING,
    )
    assert not storage.update_mission_task_fields(
        task["id"],
        mission["user_id"],
        status=TaskStatus.COMPENSATED,
    )
    assert storage.get_mission_tasks(mission["id"], mission["user_id"])[0]["status"] == "pending"


def test_retry_budget_exhaustion_rejects_claim_without_moving_counters(storage) -> None:
    mission, task = _mission_with_task(storage)
    with storage.transaction() as conn:
        conn.execute("UPDATE missions SET budget_retries=1 WHERE id=?", (mission["id"],))

    assert storage.claim_mission_task(
        task["id"],
        mission["user_id"],
        mission_id=mission["id"],
        expected_attempt=int(task["attempts"]),
    )
    first_owner = storage.get_mission_tasks(mission["id"], mission["user_id"])[0]
    assert storage.update_mission_task_fields(
        task["id"],
        mission["user_id"],
        expected_statuses=(TaskStatus.RUNNING.value,),
        expected_attempt=int(first_owner["attempts"]),
        status=TaskStatus.PENDING.value,
        started_at="",
    )
    assert storage.claim_mission_task(
        task["id"],
        mission["user_id"],
        mission_id=mission["id"],
        expected_attempt=int(first_owner["attempts"]),
    )
    retry_owner = storage.get_mission_tasks(mission["id"], mission["user_id"])[0]
    assert storage.update_mission_task_fields(
        task["id"],
        mission["user_id"],
        expected_statuses=(TaskStatus.RUNNING.value,),
        expected_attempt=int(retry_owner["attempts"]),
        status=TaskStatus.PENDING.value,
        started_at="",
    )

    for _ in range(2):
        assert not storage.claim_mission_task(
            task["id"],
            mission["user_id"],
            mission_id=mission["id"],
            expected_attempt=int(retry_owner["attempts"]),
        )
    exhausted_task = storage.get_mission_tasks(mission["id"], mission["user_id"])[0]
    exhausted_mission = storage.get_mission(mission["id"], mission["user_id"])
    assert exhausted_task["status"] == TaskStatus.PENDING.value
    assert exhausted_task["attempts"] == 2
    assert exhausted_mission["spent_retries"] == exhausted_mission["budget_retries"] == 1


def test_cancel_atomically_preserves_unknown_effect(storage) -> None:
    mission, task = _mission_with_task(storage)
    assert storage.claim_mission_task(
        task["id"],
        mission["user_id"],
        mission_id=mission["id"],
        expected_attempt=int(task["attempts"]),
    )
    claimed = storage.get_mission_tasks(mission["id"], mission["user_id"])[0]
    assert storage.update_mission_task_fields(
        task["id"],
        mission["user_id"],
        expected_statuses=(TaskStatus.RUNNING.value,),
        expected_attempt=int(claimed["attempts"]),
        side_effect=1,
        checkpoint_json='{"tool":"web_research","arguments":{"query":"opaque"}}',
    )

    assert storage.cancel_mission_and_tasks(mission["id"], mission["user_id"])
    cancelled = storage.get_mission(mission["id"], mission["user_id"])
    recovered = storage.get_mission_tasks(mission["id"], mission["user_id"])[0]
    assert cancelled["status"] == MissionStatus.CANCELLED.value
    assert recovered["status"] == TaskStatus.UNCERTAIN.value
    assert recovered["checkpoint_json"]
    assert not storage.claim_mission_task(
        task["id"],
        mission["user_id"],
        mission_id=mission["id"],
        expected_attempt=int(recovered["attempts"]),
    )


def test_verified_absent_effect_reopens_with_a_clean_attempt_fence(settings, storage) -> None:
    mission, task = _mission_with_task(storage)
    assert storage.claim_mission_task(
        task["id"],
        mission["user_id"],
        mission_id=mission["id"],
        expected_attempt=int(task["attempts"]),
    )
    claimed = storage.get_mission_tasks(mission["id"], mission["user_id"])[0]
    assert storage.update_mission_task_fields(
        task["id"],
        mission["user_id"],
        expected_statuses=(TaskStatus.RUNNING.value,),
        expected_attempt=int(claimed["attempts"]),
        side_effect=1,
        checkpoint_json='{"tool":"s6_absent_effect","arguments":{}}',
        compensation="manual old action",
    )
    assert storage.update_mission_task_fields(
        task["id"],
        mission["user_id"],
        expected_statuses=(TaskStatus.RUNNING.value,),
        expected_attempt=int(claimed["attempts"]),
        status=TaskStatus.UNCERTAIN.value,
    )
    service = _service(settings, storage)
    POSTCONDITIONS["s6_absent_effect"] = lambda *_args: (False, "not present")
    try:
        assert service._reconcile_uncertain(mission) == 1  # noqa: SLF001
    finally:
        POSTCONDITIONS.pop("s6_absent_effect", None)

    reopened = storage.get_mission_tasks(mission["id"], mission["user_id"])[0]
    assert reopened["status"] == TaskStatus.PENDING.value
    assert reopened["side_effect"] == 0
    assert reopened["checkpoint_json"] == "{}"
    assert reopened["compensation"] == ""
    assert not storage.update_mission_task_fields(
        task["id"],
        mission["user_id"],
        status=TaskStatus.COMPENSATED.value,
    ), "a stale human action closed work that had already been safely reopened"


@pytest.mark.parametrize(
    "deadline",
    [
        "2020-01-01T00:00:00+00:00",
        "not-a-deadline",
    ],
    ids=["expired", "malformed"],
)
def test_expired_or_malformed_parent_cannot_be_claimed(storage, deadline: str) -> None:
    mission, task = _mission_with_task(storage)
    with storage.transaction() as conn:
        conn.execute("UPDATE missions SET deadline_at=? WHERE id=?", (deadline, mission["id"]))

    assert not storage.claim_mission_task(
        task["id"],
        mission["user_id"],
        mission_id=mission["id"],
        expected_attempt=int(task["attempts"]),
    )
    row = storage.get_mission_tasks(mission["id"], mission["user_id"])[0]
    assert row["status"] == TaskStatus.PENDING.value
    assert row["attempts"] == 0


def test_terminal_mission_and_task_states_cannot_be_resurrected(storage) -> None:
    mission, task = _mission_with_task(storage)
    assert storage.update_mission_fields(
        mission["id"],
        mission["user_id"],
        status=MissionStatus.CANCELLED.value,
    )
    assert not storage.update_mission_fields(
        mission["id"],
        mission["user_id"],
        status=MissionStatus.RUNNING.value,
    )
    assert storage.update_mission_task_fields(
        task["id"],
        mission["user_id"],
        status=TaskStatus.SKIPPED.value,
    )
    assert not storage.update_mission_task_fields(
        task["id"],
        mission["user_id"],
        status=TaskStatus.PENDING.value,
    )
    assert storage.get_mission(mission["id"], mission["user_id"])["status"] == "cancelled"
    assert storage.get_mission_tasks(mission["id"], mission["user_id"])[0]["status"] == "skipped"


def test_set_plan_rejects_cross_owner_tasks_before_mutation(storage) -> None:
    first, original = _mission_with_task(storage, user_id="alice")
    second, _ = _mission_with_task(storage, user_id="bob")
    counterfeit = MissionTask(
        id=new_id("mst"),
        mission_id=second["id"],
        user_id="bob",
        seq=2,
        instruction="чужой шаг",
    )

    with pytest.raises(ValueError, match="ownership"):
        storage.set_mission_plan(
            first["id"],
            "alice",
            [counterfeit],
            plan_summary="подмена",
            status=MissionStatus.READY,
        )

    assert [row["id"] for row in storage.get_mission_tasks(first["id"], "alice")] == [original["id"]]
    assert [row["id"] for row in storage.get_mission_tasks(second["id"], "bob")] != [counterfeit.id]


def test_cancel_while_planning_cannot_resurrect_terminal_mission(storage) -> None:
    mission, _ = _mission_with_task(storage)
    assert storage.update_mission_fields(
        mission["id"],
        mission["user_id"],
        status=MissionStatus.CANCELLED.value,
    )
    replacement = MissionTask(
        id=new_id("mst"),
        mission_id=mission["id"],
        user_id=mission["user_id"],
        seq=2,
        instruction="поздний план",
    )

    assert (
        storage.set_mission_plan(
            mission["id"],
            mission["user_id"],
            [replacement],
            plan_summary="поздний план",
            status=MissionStatus.READY,
        )
        is None
    )
    assert storage.get_mission(mission["id"], mission["user_id"])["status"] == "cancelled"
    assert replacement.id not in {
        row["id"] for row in storage.get_mission_tasks(mission["id"], mission["user_id"])
    }


@pytest.mark.asyncio
async def test_runner_stops_when_durable_task_claim_is_lost(settings, storage, monkeypatch) -> None:
    mission, task = _mission_with_task(storage)
    service = _service(settings, storage)
    calls = 0

    monkeypatch.setattr(storage, "claim_mission_task", lambda *_args, **_kwargs: False)

    async def execute(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return "не должно исполниться", []

    monkeypatch.setattr(service, "_execute_task", execute)
    await service._run_task(mission, task, {1: task})  # noqa: SLF001

    assert calls == 0
    assert storage.get_mission_tasks(mission["id"], mission["user_id"])[0]["attempts"] == 0


@pytest.mark.parametrize(
    "failure_type",
    [RuntimeError, asyncio.CancelledError],
    ids=["exception", "cancelled"],
)
@pytest.mark.asyncio
async def test_post_checkpoint_failure_is_uncertain_and_never_replayed(
    settings,
    storage,
    monkeypatch,
    failure_type: type[BaseException],
) -> None:
    mission, task = _mission_with_task(storage)
    service = _service(settings, storage)
    effect_calls = 0

    async def interrupted(*_args, **_kwargs):
        nonlocal effect_calls
        effect_calls += 1
        assert storage.update_mission_task_fields(
            task["id"],
            mission["user_id"],
            side_effect=1,
            checkpoint_json='{"tool":"web_research","arguments":{"query":"opaque"}}',
            compensation="",
        )
        raise failure_type("interrupted after durable checkpoint")

    monkeypatch.setattr(service, "_execute_task", interrupted)
    if failure_type is asyncio.CancelledError:
        with pytest.raises(asyncio.CancelledError):
            await service._run_task(mission, task, {1: task})  # noqa: SLF001
    else:
        await service._run_task(mission, task, {1: task})  # noqa: SLF001

    recovered = storage.get_mission_tasks(mission["id"], mission["user_id"])[0]
    assert recovered["status"] == TaskStatus.UNCERTAIN.value
    assert recovered["attempts"] == 1
    assert "web_research" in recovered["checkpoint_json"]

    await service._advance_mission(  # noqa: SLF001
        storage.get_mission(mission["id"], mission["user_id"])
    )
    assert effect_calls == 1


@pytest.mark.asyncio
async def test_cancelled_parent_rejects_stale_task_start(settings, storage, monkeypatch) -> None:
    mission, task = _mission_with_task(storage)
    service = _service(settings, storage)
    calls = 0
    assert storage.update_mission_fields(
        mission["id"],
        mission["user_id"],
        status=MissionStatus.CANCELLED.value,
    )

    async def execute(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return "не должно исполниться", []

    monkeypatch.setattr(service, "_execute_task", execute)
    await service._run_task(mission, task, {1: task})  # noqa: SLF001

    assert calls == 0
    row = storage.get_mission_tasks(mission["id"], mission["user_id"])[0]
    assert row["status"] == TaskStatus.PENDING.value
    assert row["attempts"] == 0


def test_future_started_at_cannot_wedge_recovery(settings, storage) -> None:
    mission, task = _mission_with_task(storage)
    future = (datetime.now(UTC) + timedelta(days=30)).isoformat()
    with storage.transaction() as conn:
        conn.execute(
            "UPDATE mission_tasks SET status='running', started_at=? WHERE id=?",
            (future, task["id"]),
        )
    service = _service(settings, storage)

    assert service._reclaim_stale_tasks(mission) == 0  # noqa: SLF001
    normalized = storage.get_mission_tasks(mission["id"], mission["user_id"])[0]
    assert normalized["status"] == "running"
    assert datetime.fromisoformat(normalized["started_at"]) <= datetime.now(UTC)

    old = (datetime.now(UTC) - timedelta(hours=3)).isoformat()
    with storage.transaction() as conn:
        conn.execute("UPDATE mission_tasks SET started_at=? WHERE id=?", (old, task["id"]))
    assert service._reclaim_stale_tasks(mission) == 1  # noqa: SLF001
    assert storage.get_mission_tasks(mission["id"], mission["user_id"])[0]["status"] == "pending"
