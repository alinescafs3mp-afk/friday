"""«Напомни мне завтра в 15:00» — базовая просьба к помощнику.

Найдено тотальным аудитом по сценариям руководителя: инструмента напоминаний у
Пятницы не было вовсе. Замерено на живом прогоне — модель уходила в
`memory_search`, отвечала пересказом найденных документов, и событие в графе не
появлялось; в другой раз отвечала «Запомнил» и не делала ничего.

Ничего нового изобретать не пришлось: орган напоминаний каждый день читает
события из графа и рассылает по ним сообщения. Не хватало способа положить туда
событие словами человека.
"""

from __future__ import annotations

import asyncio
from datetime import date, timedelta

import pytest

from friday.execution_kernel import ExecutionKernel
from friday.ingestion import IngestionPipeline
from friday.knowledge_graph import KnowledgeGraph
from friday.permissions import AuthorizationService
from friday.web_surfer import WebSurfer


@pytest.fixture
def kernel(settings, storage):
    storage.ensure_user("boss", preset_key="owner")
    auth = AuthorizationService(storage)
    graph = KnowledgeGraph(storage)
    core = ExecutionKernel(auth, settings)
    core.bind_services(storage, graph, WebSurfer(settings), IngestionPipeline(settings, storage, graph))
    return core, auth.actor_for_user("boss", source="test"), storage


def _remind(kernel, what: str, when: str):
    core, actor, _ = kernel
    return asyncio.run(core.execute("remind", {"what": what, "when": when}, actor=actor))


def test_the_tool_exists_and_is_offered_to_the_model(kernel):
    """Мутация: убрать регистрацию `remind` — тест краснеет.

    Инструмент, о котором модель не знает, не существует: до этой правки
    просьба «напомни» уходила в поиск по архиву.
    """
    core, actor, _ = kernel
    names = {
        str((tool.get("function") or {}).get("name") or tool.get("name"))
        for tool in core.get_tool_definitions(actor)
    }
    assert "remind" in names, "модели не предложен инструмент напоминаний"


def test_a_reminder_becomes_an_event_the_organ_will_find(kernel):
    core, actor, storage = kernel
    result = _remind(kernel, "совещание по поверке", "завтра")
    assert result.success, result.to_llm_message()
    assert result.data["created"] is True

    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    assert result.data["on"] == tomorrow

    # Именно так его находит орган напоминаний — по временной привязке события.
    events = storage.list_events_in_range(actor.user_id, start=tomorrow, end=tomorrow)
    assert [item for item in events if "совещание" in str(item.get("name"))], (
        "событие не попало в ленту, по которой рассылаются напоминания"
    )


def test_a_named_weekday_means_the_next_one_not_the_last(kernel):
    """Мутация: убрать сдвиг вперёд — тест краснеет.

    Разбор времени писался для вопросов о ПРОШЛОМ («что было в понедельник») и
    берёт ближайший прошедший день. Замерено: «не дай забыть в понедельник
    позвонить» поставило событие на прошлую неделю — то есть не сработает
    никогда.
    """
    result = _remind(kernel, "позвонить в часть", "в понедельник")
    assert result.data["created"] is True
    planned = date.fromisoformat(result.data["on"])
    assert planned >= date.today(), f"напоминание поставлено в прошлое: {planned}"
    assert planned.weekday() == 0, "это не понедельник"
    assert (planned - date.today()).days <= 7


def test_a_day_that_already_passed_is_refused_honestly(kernel):
    """Прошедший день — не напоминание, и говорить об этом надо прямо."""
    result = _remind(kernel, "сдать отчёт", "25 июля 2020")
    assert result.data["created"] is False
    assert "прош" in str(result.data.get("reason") or "").casefold()
    assert result.data.get("hint")


def test_an_unparsable_time_says_so_instead_of_inventing_one(kernel):
    result = _remind(kernel, "позвонить", "как-нибудь потом")
    assert result.data["created"] is False
    assert "не разобрала" in str(result.data.get("reason") or "")


def test_an_empty_subject_is_refused(kernel):
    result = _remind(kernel, "   ", "завтра")
    assert result.data["created"] is False


def test_the_hour_survives_in_the_text(kernel):
    """Рассылка идёт по дням, поэтому час должен остаться словами."""
    result = _remind(kernel, "совещание", "завтра в 15:00")
    assert result.data["created"] is True
    assert result.data.get("at") == "15:00"
