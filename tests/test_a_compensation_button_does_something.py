"""Кнопка «Подтвердить» на откате доводит дело до конца.

Цепочка компенсации была оборвана посередине. Заявка создавалась с
`tool="mission_compensation"`, а такого инструмента в ядре не существовало:
`execute_approved` находил `None` и отвечал «Инструмент недоступен». Человек,
нажавший «Подтвердить», получал отказ, который ничего не объясняет; заявка
оставалась одобренной и неиспользованной, шаг миссии — вечно `uncertain`, а статус
`compensated` не проставлял никто и никогда — объявлен в модели, разрешён схемой,
ноль записей в коде.

Это тот же класс, что `default_requires_hitl`: обещание в коде без механизма за
ним. И цена та же — человек уходит уверенным, что дело сделано.

Откат система и теперь не исполняет: текст компенсации написан для человека, а
автоматический откат того, чего, может быть, и не было, — такой же необратимый шаг,
как повтор. Меняется другое: подтверждение теперь ЗНАЧИТ что-то («я разобрался,
шаг закрыть»), и ровно это написано человеку в заявке — иначе он жал бы кнопку,
ожидая, что откатит Пятница.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from friday.execution_kernel import ExecutionKernel
from friday.executive.service import ExecutiveService
from friday.ingestion import IngestionPipeline
from friday.knowledge_graph import KnowledgeGraph
from friday.permissions import AuthorizationService
from friday.storage.models import TaskStatus, new_id, utc_now
from friday.web_surfer import WebSurfer


def _kernel(settings, storage):
    auth = AuthorizationService(storage)
    graph = KnowledgeGraph(storage)
    kernel = ExecutionKernel(auth, settings)
    kernel.bind_services(storage, graph, WebSurfer(settings), IngestionPipeline(settings, storage, graph))
    return kernel, auth


def _interrupted_step(storage, *, owner: str = "alice") -> tuple[str, str]:
    """Миссия с шагом, который оборвался рядом с побочным эффектом."""
    storage.ensure_user(owner)
    now = utc_now()
    mission_id = new_id("mis")
    task_id = new_id("mst")
    long_ago = (datetime.now(UTC) - timedelta(hours=3)).isoformat()
    with storage.transaction() as conn:
        conn.execute(
            """INSERT INTO missions(id, user_id, goal, created_by, created_at, updated_at)
               VALUES(?, ?, ?, ?, ?, ?)""",
            (mission_id, owner, "объединить карточки", f"agent:{owner}", now, now),
        )
        conn.execute(
            """INSERT INTO mission_tasks(
                   id, mission_id, user_id, seq, title, instruction, status, started_at,
                   side_effect, compensation, checkpoint_json, created_at, updated_at)
               VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                task_id,
                mission_id,
                owner,
                1,
                "объединить сущности",
                "объединить сущности",
                "running",
                long_ago,
                1,
                "разъединить сущности ent_1 и ent_2",
                '{"entity_a": "ent_1", "entity_b": "ent_2"}',
                now,
                now,
            ),
        )
    return mission_id, task_id


@pytest.mark.asyncio
async def test_confirming_the_rollback_closes_the_step(settings, storage):
    """Полный путь: обрыв → заявка → решение человека → закрытый шаг.

    Мутация: снять регистрацию `mission_compensation` — тест краснеет на
    «Инструмент недоступен», ровно с той ошибкой, которую видел человек.
    """
    storage.ensure_user("alice", preset_key="owner")
    mission_id, task_id = _interrupted_step(storage)

    service = object.__new__(ExecutiveService)
    service.storage = storage
    ExecutiveService._reclaim_stale_tasks(service, {"id": mission_id, "user_id": "alice"})  # noqa: SLF001

    pending = storage.list_action_approvals("alice", status="pending")
    assert pending, "откат не предложен человеку"
    approval = pending[0]
    assert approval["tool"] == "mission_compensation"

    # Человек читает заявку и решает.
    storage.decide_action_approval(approval["id"], "alice", decision="approve", decided_by="alice")
    kernel, auth = _kernel(settings, storage)
    actor = auth.actor_for_user("alice", source="test")
    result = await kernel.execute_approved(approval["id"], actor=actor)

    assert result.success is True, f"подтверждение отката закончилось отказом: {result.error!r}"
    task = next(item for item in storage.get_mission_tasks(mission_id, "alice") if item["id"] == task_id)
    assert task["status"] == TaskStatus.COMPENSATED.value, (
        f"шаг остался в {task['status']!r} — человек решил, а система не запомнила"
    )
    assert "разъединить" in str(task.get("result") or "")


def test_the_person_is_told_that_friday_will_not_roll_back(settings, storage):
    """Текст заявки не обещает того, чего система не сделает.

    «Предлагаемый откат: …» читается как «нажми — откачу». Кнопка без объяснения
    дороже отсутствия кнопки: человек уходит уверенным, что дело сделано, и не
    делает его сам.
    """
    storage.ensure_user("alice", preset_key="owner")
    mission_id, _task_id = _interrupted_step(storage)

    service = object.__new__(ExecutiveService)
    service.storage = storage
    ExecutiveService._reclaim_stale_tasks(service, {"id": mission_id, "user_id": "alice"})  # noqa: SLF001

    summary = str(storage.list_action_approvals("alice", status="pending")[0].get("summary") or "")
    assert "РУКАМИ" in summary, f"человеку не сказано, что откат за ним: {summary!r}"
    assert "не выполнит" in summary


@pytest.mark.asyncio
async def test_a_compensation_still_waits_for_a_person(settings, storage):
    """Модель не закрывает оборвавшийся шаг сама.

    Ошибка в эту сторону дороже неудобства: «разобрался» может сказать только тот,
    кто способен посмотреть на мир, а модель посмотреть не может — она предположит.
    """
    storage.ensure_user("alice", preset_key="owner")
    mission_id, task_id = _interrupted_step(storage)
    kernel, auth = _kernel(settings, storage)
    actor = auth.actor_for_user("alice", source="test")

    direct = await kernel.execute(
        "mission_compensation", {"mission_id": mission_id, "task_id": task_id}, actor=actor
    )
    assert "approval_id" in (direct.data or {}), "шаг закрылся без человека"
    task = next(item for item in storage.get_mission_tasks(mission_id, "alice") if item["id"] == task_id)
    assert task["status"] != TaskStatus.COMPENSATED.value


@pytest.mark.asyncio
async def test_a_stranger_cannot_close_someone_elses_step(settings, storage):
    """Чужая миссия отвечает тем же, чем несуществующая."""
    storage.ensure_user("alice", preset_key="owner")
    storage.ensure_user("bob", preset_key="user")
    mission_id, task_id = _interrupted_step(storage)

    kernel, auth = _kernel(settings, storage)
    bob = auth.actor_for_user("bob", source="test")
    result = await kernel.execute(
        "mission_compensation", {"mission_id": mission_id, "task_id": task_id}, actor=bob
    )
    # У Боба нет ни права, ни миссии — важно, что шаг остаётся нетронутым.
    task = next(item for item in storage.get_mission_tasks(mission_id, "alice") if item["id"] == task_id)
    assert task["status"] != TaskStatus.COMPENSATED.value, "чужой закрыл шаг чужой миссии"
    assert result.success is False or "approval_id" in (result.data or {})
