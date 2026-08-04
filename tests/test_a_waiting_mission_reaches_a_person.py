"""Миссия, ждущая решения, доходит до человека НА ЛЮБОЙ дороге.

ЗАМЕРЕНО 2026-08-04. Миссию заводили пятью путями и смотрели постусловие — есть
ли строка в очереди уведомлений:

    инструмент  created_by=`agent:<own_id>`      НЕ ДОШЛО
    HTTP        created_by=`api`                 НЕ ДОШЛО
    мост        created_by=`telegram-bridge`     НЕ ДОШЛО
    человек     created_by=`<own_id>`            дошло
    воркер      created_by=`worker:...`          НЕ ДОШЛО

То есть механизм работал ровно там, где автор случайно совпал с идентификатором
учётной записи, и не работал на ГЛАВНОМ пути — когда миссию предложила модель.

В живой базе владельца это видно прямо: миссия в статусе `proposed` от 26 июля
висит неделями, уведомлений о миссиях за всё время — ноль. Узнать о ней можно
было, только набрав `/missions`, то есть спросив о том, о чём не знаешь.

Причина: чат искали по `created_by` как по идентификатору человека, а там лежит
то происхождение, то имя канала. Разбирать надо строку, а не подставлять
`user_id` вслепую: `agent:` несёт человека в хвосте, `worker:` не несёт никого,
имена каналов людьми не являются.
"""

from __future__ import annotations

import json

import pytest

from friday.execution_kernel import ExecutionKernel
from friday.executive import ExecutiveService
from friday.ingestion import IngestionPipeline
from friday.knowledge_graph import KnowledgeGraph
from friday.permissions import AuthorizationService
from friday.storage.models import MissionStatus
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
    storage.update_user("tenant", metadata_json=json.dumps({"chat_id": "42"}))
    auth = AuthorizationService(storage)
    graph = KnowledgeGraph(storage)
    ingestion = IngestionPipeline(settings, storage, graph)
    kernel = ExecutionKernel(auth, settings)
    kernel.bind_services(storage, graph, WebSurfer(settings), ingestion)
    service = ExecutiveService(settings, storage, auth, kernel, _Planner(), ingestion)
    kernel.bind_executive(service)
    return service


def _pushed_for(storage, mission_id: str) -> list[dict]:
    return [
        row
        for row in storage.list_pending_notifications(limit=50)
        if row["kind"] == "mission" and row["dedup_key"] == f"mission:{mission_id}"
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("road", "created_by"),
    [
        ("инструмент модели", "agent:tenant"),
        ("HTTP-маршрут", "api"),
        ("телеграм-мост", "telegram-bridge"),
        ("сам человек", "tenant"),
        ("воркер", "worker:mission_proposer"),
    ],
)
@pytest.mark.asyncio
async def test_every_road_reaches_a_person(storage, executive, road: str, created_by: str) -> None:
    """Мутация: вернуть поиск чата по сырому `created_by` — четыре дороги краснеют."""
    made = await executive.create_mission(
        "tenant", f"Цель для {road}", origin="agent", created_by=created_by
    )

    assert made["status"] == MissionStatus.PROPOSED.value
    assert _pushed_for(storage, made["id"]), f"дорога «{road}» оставила миссию незамеченной"


@pytest.mark.asyncio
async def test_a_running_mission_is_not_announced(storage, executive) -> None:
    """Обратная сторона: сообщать не о каждой миссии, а о ждущей РЕШЕНИЯ.

    Про `ready` и `running` система работает сама, и уведомление было бы шумом —
    а шум обесценивает те сообщения, которые по делу.
    """
    made = await executive.create_mission("tenant", "Обычная работа", created_by="tenant")

    assert made["status"] == MissionStatus.READY.value
    assert not _pushed_for(storage, made["id"])


@pytest.mark.asyncio
async def test_a_stranger_id_falls_back_to_the_tenant(storage, executive) -> None:
    """Автор не опознан — письмо идёт арендатору, а не пропадает.

    Молчание тут хуже промаха: миссия ждёт решения, которое иначе некому принять.
    У личной установки арендатор и есть владелец.
    """
    made = await executive.create_mission(
        "tenant", "Цель от неизвестного", origin="agent", created_by="agent:никого-такого-нет"
    )

    assert _pushed_for(storage, made["id"]), "миссия от неопознанного автора пропала молча"


@pytest.mark.asyncio
async def test_in_a_shared_archive_the_author_gets_the_letter(storage, executive) -> None:
    """Письмо идёт АВТОРУ, а не владельцу архива.

    Здесь и видна цена разбора `agent:<own_id>`. При одном арендаторе его
    отсутствие незаметно: запасной путь всё равно приводит к тому же чату, и
    мутация «не разбирать префикс» переживает проверку. Разница появляется в
    общем архиве, где `user_id` у всех один: без разбора предложение уходит
    владельцу архива, а тот, кто его затронул, не узнаёт о нём вовсе.
    """
    storage.ensure_user("person-a", preset_key="user")
    storage.update_user("person-a", metadata_json=json.dumps({"chat_id": "5001"}))

    made = await executive.create_mission(
        "tenant", "Цель участника", origin="agent", created_by="agent:person-a"
    )

    pushed = _pushed_for(storage, made["id"])
    assert pushed, "миссия участника не дошла ни до кого"
    assert pushed[0]["user_id"] == "person-a", "письмо ушло владельцу архива вместо автора"
    assert pushed[0]["chat_id"] == "5001"


@pytest.mark.asyncio
async def test_the_http_road_records_the_person_not_the_channel(settings, storage) -> None:
    """`created_by` на HTTP-дороге обязан нести человека.

    По этому полю не только ищется чат, но и ключуется дедуп миссий: с именем
    канала в общем архиве просьбы разных людей склеились бы в одну.
    """
    import inspect

    from friday.executive import api

    source = inspect.getsource(api.create_mission)

    # Сверяется ВЫЗОВ, а не любое упоминание: первая редакция искала подстроку
    # «actor.source» во всём тексте функции и покраснела от комментария, который
    # эту ошибку объясняет.
    assert "created_by=actor.own_id" in source
    assert "created_by=actor.source" not in source, "автором миссии снова записывается канал"
