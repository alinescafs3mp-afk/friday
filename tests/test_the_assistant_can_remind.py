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

from friday.execution_kernel import ExecutionKernel, _future_day
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


def test_a_yearless_calendar_date_means_its_next_occurrence() -> None:
    """The schema advertises ``3 августа`` without making the model add a year."""

    today = date(2026, 8, 8)

    assert _future_day("3 августа", today=today) == "2027-08-03"
    assert _future_day("3 августа в 15:00", today=today) == "2027-08-03"
    # A year the person actually named is never silently rewritten.
    assert _future_day("3 августа 2026", today=today) == "2026-08-03"
    # Leap-day lookup skips non-leap years instead of widening or failing.
    assert _future_day("29 февраля", today=today) == "2028-02-29"


def test_an_unparsable_time_says_so_instead_of_inventing_one(kernel):
    result = _remind(kernel, "позвонить", "как-нибудь потом")
    assert result.data["created"] is False
    assert "не разобрала" in str(result.data.get("reason") or "")


def test_an_empty_subject_is_refused(kernel):
    result = _remind(kernel, "   ", "завтра")
    assert result.data["created"] is False


def test_the_hour_survives_in_the_text(kernel):
    """The exact clock is persisted for the delivery scanner."""
    result = _remind(kernel, "совещание", "завтра в 15:00")
    assert result.data["created"] is True
    assert result.data.get("at") == "15:00"
    _, actor, storage = kernel
    from friday.storage._graph import _bounded_visible_timeline_event_rows

    events = _bounded_visible_timeline_event_rows(storage, actor.user_id, actor.own_id)
    event = next(item for item in events if item["entity_id"] == result.data["entity_id"])
    assert event["description"] == "friday-reminder-clock:15:00"


@pytest.mark.parametrize(
    ("spoken", "expected"),
    [
        ("завтра в 10 утра", "10:00"),
        ("завтра в 3 дня", "15:00"),
        ("завтра в 8 вечера", "20:00"),
        ("завтра в 12 ночи", "00:00"),
    ],
)
def test_spoken_day_periods_are_persisted_as_an_exact_clock(kernel, spoken: str, expected: str):
    result = _remind(kernel, "проверить расписание", spoken)

    assert result.data["created"] is True
    assert result.data["at"] == expected


def test_an_exact_clock_earlier_today_is_refused(kernel):
    result = _remind(kernel, "опоздавшее дело", "сегодня в 00:00")

    assert result.data["created"] is False
    assert "прош" in str(result.data.get("reason") or "").casefold()


def test_a_saved_reminder_does_not_claim_chat_delivery_without_a_route(kernel):
    result = _remind(kernel, "совещание", "завтра")

    assert result.data["created"] is True
    assert result.data["delivery_scheduled"] is False


def test_two_equal_subjects_are_two_scheduled_effects(kernel):
    first = _remind(kernel, "одинаковая тема", "завтра")
    second = _remind(kernel, "одинаковая тема", "в понедельник")

    assert first.data["entity_id"] != second.data["entity_id"]


def test_the_kernel_returns_exactly_the_subject_it_persisted(kernel):
    core, actor, storage = kernel
    subject = "длинная тема " * 30
    result = asyncio.run(core.execute("remind", {"what": subject, "when": "завтра"}, actor=actor))

    assert len(result.data["what"]) == 120
    from friday.storage._graph import _bounded_visible_timeline_event_rows

    events = _bounded_visible_timeline_event_rows(storage, actor.user_id, actor.own_id)
    event = next(item for item in events if item["entity_id"] == result.data["entity_id"])
    assert event["name"] == result.data["what"]
