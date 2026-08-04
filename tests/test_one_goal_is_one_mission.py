"""Одна и та же цель, предложенная дважды, — одна миссия.

ЗАМЕРЕНО 2026-08-04: два вызова `mission_propose` подряд с той же целью заводили
две миссии и два набора шагов. Ничто на пути их не сравнивало — ни инструмент, ни
служба, ни таблица (у `missions` только `id PRIMARY KEY`, оба индекса не
уникальны). Дедуп заявок на подтверждение сюда не доезжает: он живёт на
`risk="high"`, а `mission_propose` объявлен `mutate`.

Чем это плохо на живой установке:

* человек получает два одинаковых «миссия ждёт запуска» и две карточки, по каждой
  из которых надо принять решение;
* при включённой полной автономии близнецы ВЫПОЛНЯЮТСЯ ОБА, каждый шаг — вызовы
  модели, каждый результат — отдельный элемент во входящих;
* бегунок активных миссий один на всех людей (потолок восемь, выборка от свежих),
  так что близнецы из одного разговора вытесняют работу остальных участников.

Отдельная проверка здесь — про то, чтобы лечение не оказалось хуже болезни.
Молчаливый дедуп («не вставили и вернули чужую строку») в этом месте недопустим:
служба возврат не читает и пошла бы дальше со своим идентификатором, породив
призрачную миссию — лишний поход планировщика в модель, запись в аудит о создании
несуществующего, второе уведомление человеку и пустоту в ответе модели.
"""

from __future__ import annotations

import json

import pytest

from friday.execution_kernel import ExecutionKernel
from friday.executive import ExecutiveService
from friday.ingestion import IngestionPipeline
from friday.knowledge_graph import KnowledgeGraph
from friday.permissions import AuthorizationService
from friday.storage.models import MissionOrigin, MissionStatus
from friday.web_surfer import WebSurfer


class _CountingPlanner:
    """Планировщик, который считает, сколько раз его позвали.

    Число вызовов — наблюдаемое постусловие: дедуп обязан выходить ДО планирования,
    иначе он тратит поход в модель на работу, которую выбросит.
    """

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
                        "title": "План",
                        "tasks": [
                            {
                                "seq": 1,
                                "kind": "gather",
                                "title": "Сбор",
                                "instruction": "Собери факты",
                                "depends_on": [],
                            }
                        ],
                    },
                    ensure_ascii=False,
                )
            }
        return {"content": "Готовый результат шага."}


@pytest.fixture
def executive(settings, storage):
    storage.ensure_user("alice", preset_key="owner")
    auth = AuthorizationService(storage)
    graph = KnowledgeGraph(storage)
    ingestion = IngestionPipeline(settings, storage, graph)
    kernel = ExecutionKernel(auth, settings)
    kernel.bind_services(storage, graph, WebSurfer(settings), ingestion)
    llm = _CountingPlanner()
    service = ExecutiveService(settings, storage, auth, kernel, llm, ingestion)
    kernel.bind_executive(service)
    return service, kernel, auth, llm


def _missions(storage, user_id: str = "alice") -> int:
    return storage.execute(
        "SELECT COUNT(*) AS n FROM missions WHERE user_id=?", (user_id,)
    ).fetchone()["n"]


@pytest.mark.asyncio
async def test_the_same_goal_twice_makes_one_mission(storage, executive) -> None:
    """Мутация: убрать поиск близнеца — снова две миссии и два набора шагов."""
    service, _, _, _ = executive
    goal = "Собрать документы по поверке"

    first = await service.create_mission("alice", goal, created_by="agent:person-a")
    second = await service.create_mission("alice", goal, created_by="agent:person-a")

    assert _missions(storage) == 1, "одна просьба завела две миссии"
    assert second["id"] == first["id"]
    assert second.get("existing") is True, "повтор не назван повтором"


@pytest.mark.asyncio
async def test_the_repeat_does_not_spend_the_planner(storage, executive) -> None:
    """Выход обязан быть ДО планирования, и это не только про экономию.

    `set_mission_plan` начинается с удаления шагов миссии. Дедуп, который вернул бы
    существующую миссию и поехал дальше, стёр бы шаги ИДУЩЕЙ работы вместе с их
    результатами.
    """
    service, _, _, llm = executive
    goal = "Подготовить сводку по входящим"

    await service.create_mission("alice", goal, created_by="agent:person-a")
    spent = llm.calls
    await service.create_mission("alice", goal, created_by="agent:person-a")

    assert llm.calls == spent, "повтор сходил к планировщику впустую"


@pytest.mark.asyncio
async def test_the_repeat_does_not_push_a_second_time(storage, executive) -> None:
    """Человеку не приходит второе одинаковое «миссия ждёт запуска».

    Очередь уведомлений дедуплицируется по `mission:{id}`, и пока идентификаторы
    были разными, разными были и ключи. То есть без этой проверки дедуп миссий
    доказывал бы половину.
    """
    service, _, _, _ = executive
    storage.update_user("alice", metadata_json=json.dumps({"chat_id": "42"}))
    goal = "Разобрать очередь слияний"

    await service.create_mission("alice", goal, created_by="alice")
    await service.create_mission("alice", goal, created_by="alice")

    pushed = [row for row in storage.list_pending_notifications(limit=20) if row["kind"] == "mission"]
    assert len(pushed) <= 1, f"человеку ушло {len(pushed)} одинаковых уведомления"


@pytest.mark.asyncio
async def test_the_repeat_answers_with_a_real_mission(storage, executive) -> None:
    """Ответ на повторе — ВИД существующей миссии, а не самодельный словарь.

    Инструмент читает `id`, телеграмный форматтер — `title` и `task_count`. Стоит
    вернуть заготовку без них, и человек получит сообщение о миссии без названия и
    без числа шагов, а модель — пустоту вместо идентификатора.
    """
    service, _, _, _ = executive
    goal = "Проверить сроки поверки"

    first = await service.create_mission("alice", goal, created_by="agent:person-a")
    again = await service.create_mission("alice", goal, created_by="agent:person-a")

    assert again["id"] == first["id"]
    assert again.get("title") and again.get("status")
    assert again.get("tasks") or again.get("task_count"), "у ответа нет шагов миссии"


@pytest.mark.asyncio
async def test_a_finished_mission_does_not_block_a_new_one(storage, executive) -> None:
    """Повторить законченную работу человек вправе — это просьба «сделай ещё раз».

    Обратная сторона дедупликации, и она важнее её самой: миссия, упавшая на
    третьем шаге, не должна навсегда закрывать дорогу той же цели.
    """
    service, _, _, _ = executive
    goal = "Сверить остатки"

    first = await service.create_mission("alice", goal, created_by="agent:person-a")
    storage.update_mission_fields(first["id"], "alice", status=MissionStatus.FAILED.value)

    again = await service.create_mission("alice", goal, created_by="agent:person-a")

    assert again["id"] != first["id"], "упавшая миссия закрыла дорогу законному повтору"
    assert _missions(storage) == 2


@pytest.mark.asyncio
async def test_a_stale_waiting_mission_does_not_block_a_new_one(storage, executive) -> None:
    """Ключ действует сутки, а не вечно.

    При выключенной автономии агентская миссия садится в `proposed` и висит до
    решения человека. Замерено на живой базе: такая миссия провисела НЕДЕЛЮ. Без
    срока июльская просьба глушила бы августовскую молча.
    """
    service, _, _, _ = executive
    goal = "Собрать хронологию по объекту"

    first = await service.create_mission("alice", goal, created_by="agent:person-a")
    with storage.transaction() as conn:
        conn.execute(
            "UPDATE missions SET created_at='2026-07-01T00:00:00+00:00' WHERE id=?",
            (first["id"],),
        )

    again = await service.create_mission("alice", goal, created_by="agent:person-a")

    assert again["id"] != first["id"], "недельной давности миссия глушит новую просьбу"


@pytest.mark.asyncio
async def test_another_persons_mission_does_not_block_mine(storage, executive) -> None:
    """В общем архиве арендатор один на всех, и людей различает только автор."""
    service, _, _, _ = executive
    goal = "Подготовить отчёт по проекту"

    mine = await service.create_mission("alice", goal, created_by="agent:person-a")
    theirs = await service.create_mission("alice", goal, created_by="agent:person-b")

    assert theirs["id"] != mine["id"], "просьба участника заглушена просьбой соседа"
    assert _missions(storage) == 2


@pytest.mark.asyncio
async def test_a_different_goal_is_a_different_mission(storage, executive) -> None:
    """Цель сравнивается точно: более узкая просьба не должна исчезать.

    «Отчёт по проекту А» и «Отчёт по проекту А за июль» — разные работы, и
    сравнение по префиксу или без учёта регистра склеило бы вторую в первую.
    """
    service, _, _, _ = executive

    wide = await service.create_mission("alice", "Отчёт по проекту А", created_by="alice")
    narrow = await service.create_mission("alice", "Отчёт по проекту А за июль", created_by="alice")

    assert narrow["id"] != wide["id"]
    assert _missions(storage) == 2


@pytest.mark.asyncio
async def test_the_tool_tells_the_model_it_is_not_new(storage, executive) -> None:
    """Признак повтора обязан доехать до модели.

    Иначе она отчитается человеку о создании новой миссии, которой не появилось, —
    то есть дедупликация починит дубли и заведёт на их месте ложное подтверждение.
    """
    _, kernel, auth, _ = executive
    actor = auth.actor_for_user("alice", source="test")
    call = {"goal": "Свести показания приборов"}

    first = await kernel.execute("mission_propose", dict(call), actor=actor)
    second = await kernel.execute("mission_propose", dict(call), actor=actor)

    assert first.success and second.success, second.error
    assert _missions(storage) == 1
    assert second.data.get("existing") is True, "модель считает повтор новой миссией"
    assert second.data.get("mission_id") == first.data.get("mission_id")


@pytest.mark.asyncio
async def test_the_worker_proposal_still_works(storage, executive) -> None:
    """Проверка того, что правка узкая: воркерная дорога не задета.

    У неё своя, более строгая защита (кулдаун плюс запрет второй незавершённой), и
    новый ключ не должен ей мешать: `created_by` там стабилен, а цель одна и та же
    по построению.
    """
    service, _, _, _ = executive

    made = await service.create_mission(
        "alice",
        "Просмотреть накопленные входящие",
        origin=MissionOrigin.WORKER,
        created_by="worker:mission_proposer",
    )

    assert made.get("id") and not made.get("existing")
