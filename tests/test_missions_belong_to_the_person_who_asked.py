"""Чужую миссию видно всем, а трогать её может только автор.

Найдено ревью уязвимых участков 2026-08-04. Четыре маршрута миссий звали службу с
`actor.user_id`, а в общем архиве это один арендатор на всех: участник набирал
/missions и получал под заголовком «Ваши миссии» цели ВСЕХ участников, и рядом с
каждой чужой строкой стояли кнопки «Запустить» и «Остановить». Остановка чужой
работы писалась в журнал под арендатором, то есть выяснить, кто её остановил,
было нельзя.

Первая правка того же дня закрыла обе половины разом — и показ, и управление.
Владелец, отвечая на прямой вопрос о границах, решил иначе: **«все видят всех»**.
Общий архив на то и общий, люди работают над одним корпусом, и не видеть, что по
нему уже идёт, значит запускать одну работу дважды.

Поэтому половины разведены:

  показ      — без границы вовсе, решение владельца;
  управление — автору миссии либо хозяину архива.

Смотреть и вмешиваться — разные права, и одно решение их не объединяет.

Отказ на чужой миссии теперь ЧЕСТНЫЙ. Пока чужие были не видны, «чужая» и
«несуществующая» обязаны были отвечать одинаково, иначе разница ответов сама
сообщала бы, что миссия есть. Теперь она стоит в списке у всех, скрывать нечего,
и «не найдена» на видимую строку было бы просто неправдой.

Автор в строке миссии записан по-разному в зависимости от дороги: через HTTP это
`own_id`, а через Пятницу — `agent:<own_id>`. Сравнение с одним `own_id`
объявляло бы чужой ту миссию, которую человек сам попросил завести в разговоре.
"""

from __future__ import annotations

import json

import pytest

from friday.execution_kernel import ExecutionKernel
from friday.executive import ExecutiveService
from friday.ingestion import IngestionPipeline
from friday.knowledge_graph import KnowledgeGraph
from friday.permissions import ActorContext, AuthorizationService
from friday.web_surfer import WebSurfer


class _Planner:
    enabled = True
    model = "stub"

    async def chat(self, messages, **kwargs):  # noqa: ANN003, ARG002
        if "планировщик миссий" in str(messages[0].get("content") or ""):
            return {
                "content": json.dumps(
                    {
                        "title": "План",
                        "tasks": [
                            {
                                "seq": 1,
                                "kind": "gather",
                                "title": "Шаг",
                                "instruction": "Сделай",
                                "depends_on": [],
                            }
                        ],
                    },
                    ensure_ascii=False,
                )
            }
        return {"content": "готово"}


@pytest.fixture
def executive(settings, storage):
    storage.ensure_user("tenant", preset_key="owner")
    auth = AuthorizationService(storage)
    graph = KnowledgeGraph(storage)
    ingestion = IngestionPipeline(settings, storage, graph)
    kernel = ExecutionKernel(auth, settings)
    kernel.bind_services(storage, graph, WebSurfer(settings), ingestion)
    service = ExecutiveService(settings, storage, auth, kernel, _Planner(), ingestion)
    kernel.bind_executive(service)
    return service


# --- служба: граница по автору существует и работает -------------------------


@pytest.mark.asyncio
async def test_the_service_can_narrow_to_one_author(executive) -> None:
    """Мутация: убрать границу по автору — управление перестаёт её иметь."""
    await executive.create_mission("tenant", "личное дело Петрова", created_by="person-a")
    await executive.create_mission("tenant", "справка по увольнению", created_by="person-b")

    mine = executive.list_mission_views("tenant", created_by="person-a")

    assert [row["goal"] for row in mine] == ["личное дело Петрова"]


@pytest.mark.asyncio
async def test_the_service_refuses_to_stop_a_foreign_mission(executive) -> None:
    """Худший исход: чужая работа остановлена, и в журнале стоит арендатор."""
    theirs = await executive.create_mission("tenant", "чужая цель", created_by="person-b")

    stopped = await executive.cancel_mission(theirs["id"], "tenant", created_by="person-a")

    assert stopped is None, "участник остановил чужую миссию"
    still = executive.get_mission_view(theirs["id"], "tenant")
    assert still["status"] != "cancelled"


@pytest.mark.asyncio
async def test_the_service_refuses_to_start_a_foreign_mission(executive) -> None:
    """Запуск чужой работы — та же граница, и её легко забыть на одной из кнопок."""
    theirs = await executive.create_mission("tenant", "чужая цель", created_by="person-b")

    started = await executive.start_mission(theirs["id"], "tenant", created_by="person-a")

    assert started is None


# --- дороги: показ без границы, управление с границей ------------------------


def _actor(person: str) -> ActorContext:
    """Участник общего архива: арендатор общий, человек — он сам."""
    return ActorContext(
        user_id="tenant",
        preset_key="owner",
        source="telegram-bridge",
        shared_tenant=True,
        person_id=person,
    )


def _request(executive, actor: ActorContext):
    class _Request:
        def __init__(self) -> None:
            self.app = type(
                "App",
                (),
                {
                    "state": type(
                        "S",
                        (),
                        {"executive": executive, "auth_service": executive.auth_service},
                    )()
                },
            )()
            self.state = type("RS", (), {"actor": actor})()

    return _Request()


@pytest.mark.asyncio
async def test_a_participant_sees_every_goal(executive) -> None:
    """Решение владельца: «все видят всех».

    Мутация: вернуть границу в `_visible_to` — тест краснеет.
    """
    from friday.executive.api import list_missions

    await executive.create_mission("tenant", "цель одного", created_by="person-a")
    await executive.create_mission("tenant", "цель другого", created_by="person-b")

    answer = await list_missions(
        _request(executive, _actor("person-a")), status=None, limit=50, offset=0
    )

    assert {row["goal"] for row in answer["items"]} == {"цель одного", "цель другого"}


@pytest.mark.asyncio
async def test_a_participant_cannot_stop_what_he_can_see(executive) -> None:
    """Видно — не значит можно. Мутация: снять проверку в `_mission_to_control`."""
    from fastapi import HTTPException

    from friday.executive.api import stop_mission

    theirs = await executive.create_mission("tenant", "чужая цель", created_by="person-b")

    with pytest.raises(HTTPException) as denied:
        await stop_mission(theirs["id"], _request(executive, _actor("person-a")))

    assert denied.value.status_code == 403, "чужая миссия остановлена участником"
    assert "чужая" in str(denied.value.detail).lower()
    assert executive.get_mission_view(theirs["id"], "tenant")["status"] != "cancelled"


@pytest.mark.asyncio
async def test_a_missing_mission_and_a_foreign_one_answer_differently(executive) -> None:
    """Раз чужая видна всем, «не найдена» на неё было бы неправдой."""
    from fastapi import HTTPException

    from friday.executive.api import start_mission

    theirs = await executive.create_mission("tenant", "чужая цель", created_by="person-b")
    request = _request(executive, _actor("person-a"))

    with pytest.raises(HTTPException) as foreign:
        await start_mission(theirs["id"], request)
    with pytest.raises(HTTPException) as missing:
        await start_mission("msn_nonexistent", request)

    assert foreign.value.status_code == 403
    assert missing.value.status_code == 404


@pytest.mark.asyncio
async def test_a_mission_asked_for_in_conversation_is_still_mine(executive) -> None:
    """Через Пятницу автор пишется `agent:<own_id>` — это тот же человек.

    Мутация: сравнивать только с `own_id` — участник теряет власть над теми
    миссиями, которые сам и попросил завести, а их у него большинство.
    """
    from friday.executive.api import stop_mission

    mine = await executive.create_mission("tenant", "моя цель", created_by="agent:person-a")

    answer = await stop_mission(mine["id"], _request(executive, _actor("person-a")))

    assert answer["mission"]["status"] == "cancelled"


@pytest.mark.asyncio
async def test_the_archive_owner_still_controls_everything(executive) -> None:
    """Обратная сторона: у хозяина архива власть остаётся."""
    from friday.executive.api import stop_mission

    theirs = await executive.create_mission("tenant", "чужая цель", created_by="person-b")

    # Арендатор в стенде называется "tenant", поэтому хозяином здесь выступает
    # актор, у которого человек и арендатор совпадают, — то же условие, что и в
    # `ActorContext.is_owner`.
    owner = ActorContext(
        user_id="tenant",
        preset_key="owner",
        source="api-token",
        shared_tenant=True,
        person_id="tenant",
    )
    answer = await stop_mission(theirs["id"], _request(executive, owner))

    assert answer["mission"]["status"] == "cancelled"


# --- права шага --------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_step_runs_with_the_authors_rights_not_the_owners(settings, storage) -> None:
    """Шаг миссии исполняется под ТЕМ, КТО ЕЁ ЗАВЁЛ.

    Найдено ревью 2026-08-04. Актор шага строился по `mission["user_id"]`, а в
    общем архиве это общий арендатор с пресетом владельца — то есть шаг получал
    ЕГО права. Участник, которому не положен веб-поиск, через миссию его исполнял;
    шаг, читающий личное, читал личное владельца.

    Проверяется сам актор, а не побочный эффект: пресет и человек в нём — это и
    есть то, что решает, какие инструменты шагу доступны.
    """
    from friday.executive.service import ExecutiveService

    storage.ensure_user("tenant", preset_key="owner")
    storage.ensure_user("person-a", preset_key="user")
    auth = AuthorizationService(storage)
    graph = KnowledgeGraph(storage)
    ingestion = IngestionPipeline(settings, storage, graph)
    kernel = ExecutionKernel(auth, settings)
    kernel.bind_services(storage, graph, WebSurfer(settings), ingestion)
    service = ExecutiveService(settings, storage, auth, kernel, _Planner(), ingestion)

    person = service._person_behind("agent:person-a", "tenant")  # noqa: SLF001
    actor = auth.actor_for_user(person, source="executive")

    assert person == "person-a", "шаг снова пошёл бы под арендатором"
    assert actor.preset_key == "user", "шаг получил пресет владельца вместо своего"


@pytest.mark.asyncio
async def test_a_worker_mission_still_runs_as_the_archive(settings, storage) -> None:
    """Обратная сторона: воркерная миссия идёт от имени архива, и это верно.

    У неё нет человека по построению; отняв у неё права арендатора, мы остановили
    бы фоновую работу целиком.
    """
    from friday.executive.service import ExecutiveService

    storage.ensure_user("tenant", preset_key="owner")
    auth = AuthorizationService(storage)
    graph = KnowledgeGraph(storage)
    ingestion = IngestionPipeline(settings, storage, graph)
    kernel = ExecutionKernel(auth, settings)
    kernel.bind_services(storage, graph, WebSurfer(settings), ingestion)
    service = ExecutiveService(settings, storage, auth, kernel, _Planner(), ingestion)

    assert service._person_behind("worker:mission_proposer", "tenant") == "tenant"  # noqa: SLF001
