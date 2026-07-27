from __future__ import annotations

import json
from dataclasses import replace

import pytest

from jericho.execution_kernel import ExecutionKernel
from jericho.executive import ExecutiveService
from jericho.executive.planner import MissionPlanner
from jericho.ingestion import IngestionPipeline
from jericho.knowledge_graph import KnowledgeGraph
from jericho.permissions import AuthorizationService
from jericho.storage.models import MissionOrigin
from jericho.web_surfer import WebSurfer


class PlanLLM:
    """Duck-typed LLM that returns a fixed two-step plan then a step result."""

    enabled = True
    model = "plan-test"

    def __init__(self) -> None:
        self.calls = 0

    async def chat(self, messages, **kwargs):
        self.calls += 1
        system = str(messages[0].get("content") or "")
        if "планировщик миссий" in system:
            return {
                "content": json.dumps(
                    {
                        "title": "Двухшаговый план",
                        "tasks": [
                            {
                                "seq": 1,
                                "kind": "gather",
                                "title": "Сбор",
                                "instruction": "Собери факты",
                                "depends_on": [],
                            },
                            {
                                "seq": 2,
                                "kind": "produce",
                                "title": "Итог",
                                "instruction": "Сведи итог",
                                "depends_on": [1],
                            },
                        ],
                    },
                    ensure_ascii=False,
                )
            }
        return {"content": "Готовый результат шага миссии."}


async def _build_executive(settings, storage, llm=None):
    auth = AuthorizationService(storage)
    graph = KnowledgeGraph(storage)
    ingestion = IngestionPipeline(settings, storage, graph)
    web = WebSurfer(settings)
    kernel = ExecutionKernel(auth, settings)
    kernel.bind_services(storage, graph, web, ingestion)
    executive = ExecutiveService(settings, storage, auth, kernel, llm or _DisabledLLM(), ingestion)
    kernel.bind_executive(executive)
    return executive, kernel, web


class _DisabledLLM:
    enabled = False
    model = "offline"

    async def chat(self, messages, **kwargs):  # pragma: no cover - never called when disabled
        raise RuntimeError("LLM is disabled")


@pytest.mark.asyncio
async def test_planner_offline_fallback_returns_single_produce_task(settings):
    planner = MissionPlanner(settings, _DisabledLLM())
    title, tasks = await planner.plan("Разобраться с проектом Atlas")
    assert len(tasks) == 1
    assert tasks[0].kind.value == "produce"
    assert "Atlas" in title


@pytest.mark.asyncio
async def test_create_mission_ready_when_autonomy_enabled(settings, storage):
    storage.ensure_user("alice", preset_key="user")
    executive, _, web = await _build_executive(settings, storage)
    try:
        mission = await executive.create_mission("alice", "Организовать заметки по Atlas")
        assert mission["status"] == "ready"
        assert mission["origin"] == "user"
        assert mission["task_count"] == 1
        assert len(mission["tasks"]) == 1
    finally:
        await web.close()


@pytest.mark.asyncio
async def test_create_mission_blocked_when_autonomy_disabled(settings, storage):
    storage.ensure_user("alice", preset_key="user")
    disabled = replace(settings, autonomy_enabled=False)
    executive, _, web = await _build_executive(disabled, storage)
    try:
        mission = await executive.create_mission("alice", "Что-нибудь сделать")
        assert mission["status"] == "blocked"
        # A blocked mission is inert: the runner refuses to advance it.
        outcome = await executive.tick()
        assert outcome["ran"] == 0
        assert outcome["skipped_reason"] == "autonomy_disabled"
    finally:
        await web.close()


@pytest.mark.asyncio
async def test_tick_runs_dag_and_routes_produce_task_to_inbox(settings, storage):
    storage.ensure_user("alice", preset_key="user")
    executive, _, web = await _build_executive(settings, storage, llm=PlanLLM())
    try:
        mission = await executive.create_mission("alice", "Свести знания по Atlas")
        assert mission["task_count"] == 2
        mission_id = mission["id"]

        # Two tasks -> two ticks (one runnable task per mission per tick).
        for _ in range(4):
            current = executive.get_mission_view(mission_id, "alice")
            if current["status"] in {"completed", "failed"}:
                break
            await executive.tick()

        final = executive.get_mission_view(mission_id, "alice")
        assert final["status"] == "completed"
        assert final["done_count"] == 2
        # Mission output is review-gated: no Knowledge Object was created directly.
        assert storage.count_knowledge_objects("alice") == 0
        pending = storage.list_inbox("alice", status="pending")
        assert len(pending) == 1
        produce_task = next(task for task in final["tasks"] if task["kind"] == "produce")
        assert produce_task["inbox_id"] == pending[0]["id"]
    finally:
        await web.close()


@pytest.mark.asyncio
async def test_tick_offline_still_completes_and_queues_review(settings, storage):
    storage.ensure_user("alice", preset_key="user")
    executive, _, web = await _build_executive(settings, storage)  # offline LLM
    try:
        mission = await executive.create_mission("alice", "Оффлайн-миссия")
        for _ in range(3):
            await executive.tick()
            if executive.get_mission_view(mission["id"], "alice")["status"] == "completed":
                break
        final = executive.get_mission_view(mission["id"], "alice")
        assert final["status"] == "completed"
        assert len(storage.list_inbox("alice", status="pending")) == 1
    finally:
        await web.close()


@pytest.mark.asyncio
async def test_cancel_mission_is_terminal_and_skips_tasks(settings, storage):
    storage.ensure_user("alice", preset_key="user")
    executive, _, web = await _build_executive(settings, storage, llm=PlanLLM())
    try:
        mission = await executive.create_mission("alice", "Отменяемая миссия")
        cancelled = await executive.cancel_mission(mission["id"], "alice")
        assert cancelled["status"] == "cancelled"
        assert all(task["status"] == "skipped" for task in cancelled["tasks"])
        # A cancelled mission is not picked up by the runner.
        outcome = await executive.tick()
        assert outcome["ran"] == 0
    finally:
        await web.close()


@pytest.mark.asyncio
async def test_tenant_isolation_other_user_cannot_see_mission(settings, storage):
    storage.ensure_user("alice", preset_key="user")
    storage.ensure_user("bob", preset_key="user")
    executive, _, web = await _build_executive(settings, storage)
    try:
        mission = await executive.create_mission("alice", "Личная миссия Алисы")
        assert executive.get_mission_view(mission["id"], "bob") is None
        assert executive.list_mission_views("bob") == []
    finally:
        await web.close()


@pytest.mark.asyncio
async def test_agent_mission_propose_tool_is_review_gated(settings, storage):
    storage.ensure_user("alice", preset_key="user")
    executive, kernel, web = await _build_executive(settings, storage)
    auth = AuthorizationService(storage)
    alice = auth.actor_for_user("alice", source="test")
    try:
        result = await kernel.execute(
            "mission_propose",
            {"goal": "Проверить дубли сущностей"},
            actor=alice,
        )
        assert result.success is True
        # Agent-proposed missions wait for the user (operator_full_autonomy is off).
        assert result.data["status"] == "proposed"
        assert result.data["queued_for_review"] is True
        mission = executive.get_mission_view(result.data["mission_id"], "alice")
        assert mission["origin"] == "agent"
    finally:
        await web.close()


@pytest.mark.asyncio
async def test_operator_full_autonomy_auto_starts_proposed_missions(settings, storage):
    storage.ensure_user("alice", preset_key="user")
    full = replace(settings, operator_full_autonomy=True)
    executive, _, web = await _build_executive(full, storage)
    try:
        mission = await executive.create_mission("alice", "Автономная миссия", origin=MissionOrigin.WORKER)
        assert mission["status"] == "ready"
    finally:
        await web.close()


def test_mission_http_endpoints_create_list_and_stop(settings):
    from fastapi.testclient import TestClient

    from jericho.server import create_app

    app = create_app(settings)
    owner_headers = {"Authorization": f"Bearer {settings.api_token}"}
    with TestClient(app) as client:
        created = client.post("/api/missions", headers=owner_headers, json={"goal": "Навести порядок в базе"})
        assert created.status_code == 200
        mission = created.json()["mission"]
        mission_id = mission["id"]
        assert mission["status"] == "ready"

        listing = client.get("/api/missions", headers=owner_headers)
        assert listing.status_code == 200
        assert any(item["id"] == mission_id for item in listing.json()["items"])

        detail = client.get(f"/api/missions/{mission_id}", headers=owner_headers)
        assert detail.status_code == 200
        assert detail.json()["mission"]["id"] == mission_id

        stopped = client.post(f"/api/missions/{mission_id}/stop", headers=owner_headers)
        assert stopped.status_code == 200
        assert stopped.json()["mission"]["status"] == "cancelled"

        empty = client.post("/api/missions", headers=owner_headers, json={"goal": "   "})
        assert empty.status_code == 400

        # Admin inspection router lists the same mission cross-tenant.
        admin_list = client.get("/api/admin/missions", headers=owner_headers)
        assert admin_list.status_code == 200
        assert any(item["id"] == mission_id for item in admin_list.json()["items"])


@pytest.mark.asyncio
async def test_backlog_proposer_dedupes_worker_missions(settings, storage):
    storage.ensure_user("alice", preset_key="user")
    executive, _, web = await _build_executive(settings, storage)
    try:
        # Below threshold: nothing proposed.
        assert await executive.maybe_propose_from_backlog("alice") is None
        for index in range(12):
            await executive.ingestion.queue_agent_candidate(
                "alice",
                f"Материал на review номер {index}",
                source_ref=f"seed:{index}",
                candidate_type="memory",
            )
        first = await executive.maybe_propose_from_backlog("alice")
        assert first is not None
        assert first["origin"] == "worker"
        # A second call must not create a duplicate worker mission.
        assert await executive.maybe_propose_from_backlog("alice") is None
        worker_missions = [
            mission for mission in executive.list_mission_views("alice") if mission["origin"] == "worker"
        ]
        assert len(worker_missions) == 1
    finally:
        await web.close()


class _DeadEndpointLLM:
    """Plans fine, then every step call fails — a model that went away mid-mission."""

    enabled = True
    model = "dead"

    async def chat(self, messages, **kwargs):
        system = str(messages[0].get("content") or "")
        if "планировщик миссий" in system:
            return {
                "content": json.dumps(
                    {
                        "title": "Один шаг",
                        "tasks": [
                            {
                                "seq": 1,
                                "kind": "produce",
                                "title": "Итог",
                                "instruction": "Сведи итог",
                                "depends_on": [],
                            }
                        ],
                    },
                    ensure_ascii=False,
                )
            }
        raise RuntimeError("connection refused")


@pytest.mark.asyncio
async def test_a_dead_model_fails_the_step_instead_of_completing_the_mission(settings, storage):
    """An unreachable endpoint must not produce a "successful" mission.

    Every round of the tool loop broke and the final synthesis raised, and the
    executor returned the literal string "Не удалось собрать результат шага
    автоматически." as the step's answer. `_run_task` could not tell that from a
    real result: it stored it as the DONE result, routed it to the Inbox as a
    knowledge candidate, and `_finalize` saw done > 0 and reported the mission
    COMPLETED. The user is told their mission finished and hands back an apology.
    """
    autonomous = replace(settings, autonomy_enabled=True)
    storage.ensure_user("alice", preset_key="user")
    executive, _, web = await _build_executive(autonomous, storage, llm=_DeadEndpointLLM())
    try:
        mission = await executive.create_mission("alice", "Собрать обзор рынка", origin=MissionOrigin.USER)
        await executive.tick()

        tasks = storage.get_mission_tasks(mission["id"], "alice")
        assert [task["status"] for task in tasks] == ["failed"]
        assert "unavailable" in str(tasks[0]["error"])
        assert not tasks[0]["result"]
        assert not tasks[0]["inbox_id"], "an apology was routed to the Inbox as knowledge"

        refreshed = storage.get_mission(mission["id"], "alice")
        assert refreshed["status"] == "failed"
        assert storage.list_inbox("alice", limit=50) == []
    finally:
        await web.close()


@pytest.mark.asyncio
async def test_a_task_stuck_in_running_is_reclaimed(settings, storage):
    """Nothing in-process can cover the process being killed mid-task.

    `_run_task` writes RUNNING and only leaves that state via its own handlers, so
    a kill between the two writes strands the row for good: `_pick_runnable` never
    selects a RUNNING task and `_finalize` needs every task terminal. There is no
    lease and no heartbeat on a mission task, so the mission is wedged forever.
    """
    from datetime import UTC, datetime, timedelta

    autonomous = replace(settings, autonomy_enabled=True)
    storage.ensure_user("alice", preset_key="user")
    executive, _, web = await _build_executive(autonomous, storage, llm=PlanLLM())
    try:
        mission = await executive.create_mission("alice", "Разобрать Atlas", origin=MissionOrigin.USER)
        task = storage.get_mission_tasks(mission["id"], "alice")[0]
        stale = (datetime.now(UTC) - timedelta(hours=3)).isoformat(timespec="seconds")
        storage.update_mission_task_fields(task["id"], "alice", status="running", started_at=stale)

        # A healthy long task must NOT be reset under itself.
        fresh = storage.get_mission_tasks(mission["id"], "alice")[-1]
        if fresh["id"] != task["id"]:
            storage.update_mission_task_fields(
                fresh["id"], "alice", status="running", started_at=datetime.now(UTC).isoformat()
            )

        await executive.tick()

        reclaimed = storage.get_mission_tasks(mission["id"], "alice")
        by_id = {item["id"]: item for item in reclaimed}
        assert by_id[task["id"]]["status"] != "running"
        if fresh["id"] != task["id"]:
            assert by_id[fresh["id"]]["status"] == "running"
    finally:
        await web.close()
