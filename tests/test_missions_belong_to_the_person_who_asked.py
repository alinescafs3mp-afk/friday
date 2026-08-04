"""«Мои миссии» — это мои, а не всех участников общего архива.

Найдено ревью уязвимых участков 2026-08-04. Четыре маршрута миссий звали службу с
`actor.user_id`, а в общем архиве это один арендатор на всех.

Что это значило на живой настройке: участник набирает /missions и получает
заголовок «Ваши миссии: N», под которым перечислены цели ВСЕХ участников. Цель —
свободный текст просьбы («собрать всё по личному делу такого-то», «подготовить
справку по увольнению»), и рядом с каждой чужой строкой стояли кнопки «Запустить»
и «Остановить». Остановка чужой работы при этом писалась в журнал под арендатором,
то есть выяснить, кто её остановил, было нельзя.

Владельцу границы нет, и это не упущение: миссии идут по его корпусу, надзор —
часть его роли. Участник видит и трогает только своё.

Столбец `created_by` для этого уже существовал; с правкой того же дня он несёт
ЧЕЛОВЕКА на всех дорогах, а не имя канала.
"""

from __future__ import annotations

import json

import pytest

from friday.execution_kernel import ExecutionKernel
from friday.executive import ExecutiveService
from friday.ingestion import IngestionPipeline
from friday.knowledge_graph import KnowledgeGraph
from friday.permissions import AuthorizationService
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


@pytest.mark.asyncio
async def test_a_participant_sees_only_his_own_goals(executive) -> None:
    """Мутация: убрать границу по автору — снова видны чужие цели."""
    await executive.create_mission("tenant", "личное дело Петрова", created_by="person-a")
    await executive.create_mission("tenant", "справка по увольнению", created_by="person-b")

    mine = executive.list_mission_views("tenant", created_by="person-a")

    assert [row["goal"] for row in mine] == ["личное дело Петрова"]


@pytest.mark.asyncio
async def test_a_participant_cannot_stop_another_persons_mission(executive) -> None:
    """Худший исход: чужая работа остановлена, и в журнале стоит арендатор."""
    theirs = await executive.create_mission("tenant", "чужая цель", created_by="person-b")

    stopped = await executive.cancel_mission(theirs["id"], "tenant", created_by="person-a")

    assert stopped is None, "участник остановил чужую миссию"
    still = executive.get_mission_view(theirs["id"], "tenant")
    assert still["status"] != "cancelled"


@pytest.mark.asyncio
async def test_a_participant_cannot_start_another_persons_mission(executive) -> None:
    """Запуск чужой работы — та же граница, и её легко забыть на одной из кнопок."""
    theirs = await executive.create_mission("tenant", "чужая цель", created_by="person-b")

    started = await executive.start_mission(theirs["id"], "tenant", created_by="person-a")

    assert started is None


@pytest.mark.asyncio
async def test_a_foreign_mission_looks_exactly_like_a_missing_one(executive) -> None:
    """Разница ответов сама сообщила бы, что миссия есть и чья она."""
    theirs = await executive.create_mission("tenant", "чужая цель", created_by="person-b")

    foreign = executive.get_mission_view(theirs["id"], "tenant", created_by="person-a")
    missing = executive.get_mission_view("msn_nonexistent", "tenant", created_by="person-a")

    assert foreign is None and missing is None


@pytest.mark.asyncio
async def test_the_owner_still_sees_everything(executive) -> None:
    """Обратная сторона: надзор владельца остаётся.

    Слишком строгая правка отняла бы у него общий обзор — а миссии идут по его
    корпусу, и это его роль, а не утечка.
    """
    await executive.create_mission("tenant", "цель одного", created_by="person-a")
    await executive.create_mission("tenant", "цель другого", created_by="person-b")

    assert len(executive.list_mission_views("tenant")) == 2


def test_the_routes_ask_only_for_mine() -> None:
    """Проверяется ПОДКЛЮЧЕНИЕ: служба умеет границу, если ей дадут человека.

    Тесты службы остались бы зелёными при маршрутах, передающих арендатора, —
    ровно так дефект и жил.
    """
    import inspect

    from friday.executive import api

    for name in ("list_missions", "get_mission", "start_mission", "stop_mission"):
        source = inspect.getsource(getattr(api, name))
        assert "_only_mine(actor)" in source, f"{name} снова идёт без человека"

    helper = inspect.getsource(api._only_mine)  # noqa: SLF001
    assert 'getattr(actor, "is_owner", False)' in helper, "владелец потерял надзор"


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
