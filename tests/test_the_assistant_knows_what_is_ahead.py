"""«Какие планы на сегодня?» — вопрос о будущем, а не о вчерашней переписке.

Найдено недельным прогоном 2026-08-02. На «Доброе утро! Какие планы на сегодня?»
Пятница вызвала `what_happened` и пересказала человеку его же ночную активность:
«в 00:37 смотрел статистику базы, в 02:31 спрашивал, как меня зовут». Инструмента,
смотрящего ВПЕРЁД, у неё не было вовсе — при том что утренний вопрос о планах для
помощника руководителя один из основных.

Лента та же, по которой рассылает орган напоминаний, и отметка автора та же:
чужие напоминания в чужие планы не попадают.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import date, timedelta

import pytest

from friday.execution_kernel import ExecutionKernel
from friday.ingestion import IngestionPipeline
from friday.knowledge_graph import KnowledgeGraph
from friday.permissions import LEGACY_OWNER_USER_ID, AuthorizationService
from friday.storage.models import EntityType
from friday.web_surfer import WebSurfer


@pytest.fixture
def kernel(settings, storage):
    storage.ensure_user("boss", preset_key="owner")
    auth = AuthorizationService(storage)
    graph = KnowledgeGraph(storage)
    core = ExecutionKernel(auth, settings)
    core.bind_services(storage, graph, WebSurfer(settings), IngestionPipeline(settings, storage, graph))
    return core, auth.actor_for_user("boss", source="test"), storage


def _ask(kernel, **params):
    core, actor, _ = kernel
    return asyncio.run(core.execute("upcoming", params, actor=actor))


def test_the_tool_is_offered_to_the_model(kernel) -> None:
    """Мутация: убрать регистрацию — тест краснеет.

    Инструмент, о котором модель не знает, не существует: до него вопрос о планах
    уходил в «что происходило».
    """
    core, actor, _ = kernel
    names = {
        str((tool.get("function") or {}).get("name") or tool.get("name"))
        for tool in core.get_tool_definitions(actor)
    }
    assert "upcoming" in names


def test_a_reminder_shows_up_in_the_plans(kernel) -> None:
    core, actor, _ = kernel
    asyncio.run(core.execute("remind", {"what": "созвон с подрядчиком", "when": "завтра"}, actor=actor))

    result = _ask(kernel)
    assert result.success, result.to_llm_message()
    assert any("созвон" in str(item["what"]) for item in result.data["items"]), result.data


def test_tomorrow_is_called_tomorrow(kernel) -> None:
    """Человеку — «завтра», а не голая дата: он спрашивал про свой день."""
    core, actor, _ = kernel
    asyncio.run(core.execute("remind", {"what": "позвонить в автосервис", "when": "завтра"}, actor=actor))

    item = next(item for item in _ask(kernel).data["items"] if "автосервис" in item["what"])
    assert item["when"] == "завтра"
    assert item["on"] == (date.today() + timedelta(days=1)).isoformat()


def test_an_empty_week_says_so_plainly(kernel) -> None:
    result = _ask(kernel)
    assert result.data["total"] == 0
    assert "ничего не запланировано" in result.data["note"]


def test_the_horizon_is_bounded(kernel) -> None:
    """Потолок есть, и он не молчаливый: возвращённое поле говорит, за сколько дней."""
    assert _ask(kernel, days=1000).data["days"] == 60
    # Ноль — это «не указано», а не «ноль дней»: модель нередко передаёт 0 вместо
    # пропуска поля, и пустой горизонт был бы ответом «планов нет» на любой вопрос.
    assert _ask(kernel, days=0).data["days"] == 7
    assert _ask(kernel, days=-5).data["days"] == 1


def test_someone_elses_reminder_is_not_in_my_plans(settings, storage) -> None:
    """В общем архиве события лежат под одним арендатором — но просьба личная."""
    shared = replace(settings, shared_archive=True)
    storage.ensure_user(LEGACY_OWNER_USER_ID, preset_key="owner")
    storage.ensure_user("telegram:test:5002", source="telegram", preset_key="owner")
    auth = AuthorizationService(storage, shared_tenant=LEGACY_OWNER_USER_ID)
    graph = KnowledgeGraph(storage)
    core = ExecutionKernel(auth, shared)
    core.bind_services(storage, graph, WebSurfer(shared), IngestionPipeline(shared, storage, graph))

    owner = auth.actor_for_user(LEGACY_OWNER_USER_ID, source="test")
    other = auth.actor_for_user("telegram:test:5002", source="telegram")
    asyncio.run(core.execute("remind", {"what": "личное дело", "when": "завтра"}, actor=other))

    mine = asyncio.run(core.execute("upcoming", {}, actor=owner))
    assert not any("личное дело" in str(item["what"]) for item in mine.data["items"]), (
        "чужое напоминание попало в мои планы"
    )


def test_an_event_from_a_document_is_everyones_business(settings, storage) -> None:
    """Событие без автора — общий материал, оно в планах хозяина архива."""
    shared = replace(settings, shared_archive=True)
    storage.ensure_user(LEGACY_OWNER_USER_ID, preset_key="owner")
    auth = AuthorizationService(storage, shared_tenant=LEGACY_OWNER_USER_ID)
    graph = KnowledgeGraph(storage)
    core = ExecutionKernel(auth, shared)
    core.bind_services(storage, graph, WebSurfer(shared), IngestionPipeline(shared, storage, graph))

    event = graph.create_entity(LEGACY_OWNER_USER_ID, "Совещание по поверке", EntityType.EVENT)
    graph.set_event_time(
        LEGACY_OWNER_USER_ID, event["id"], (date.today() + timedelta(days=2)).isoformat()
    )

    owner = auth.actor_for_user(LEGACY_OWNER_USER_ID, source="test")
    result = asyncio.run(core.execute("upcoming", {}, actor=owner))
    assert any("Совещание" in str(item["what"]) for item in result.data["items"])
