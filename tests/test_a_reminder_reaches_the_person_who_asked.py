"""Напоминание принадлежит тому, кто попросил, — даже в общем архиве.

Найдено тотальным аудитом по сценариям руководителя, на живой настройке
(`FRIDAY_SHARED_ARCHIVE=1`). Общий архив — заказ владельца: все видят документы и
записи друг друга. Сделан он подменой арендатора при выдаче актора: `user_id` у
всех один, человека называет `own_id`.

Из-за этого просьба «напомни мне завтра» создавала событие под ОБЩИМ арендатором,
а орган напоминаний перебирает людей и ищет события по каждому. Событие находилось
только у владельца общего архива. Значит напоминание, поставленное кем угодно,
приходило ВЛАДЕЛЬЦУ, а автору не приходило ничего: и не работает, и показывает
чужому личную просьбу.

События из документов (встреча, извлечённая при разборе файла) — общий материал, и
напоминать о них по-прежнему следует хозяину архива. Различает эти два случая
только отметка автора, поставленная в момент просьбы.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from friday.execution_kernel import ExecutionKernel
from friday.ingestion import IngestionPipeline
from friday.knowledge_graph import KnowledgeGraph
from friday.organs import ServiceContext
from friday.organs.reminders import scan_reminders
from friday.permissions import LEGACY_OWNER_USER_ID, AuthorizationService
from friday.storage.models import EntityType
from friday.web_surfer import WebSurfer

OWNER_CHAT = "5001"  # оба чата — из списка разрешённых в conftest
OTHER_CHAT = "5002"


def _shared_settings(settings):
    return replace(
        settings,
        shared_archive=True,
        reminders_enabled=True,
        reminders_lead_days=1,
        quiet_hours_start=0,
        quiet_hours_end=0,
    )


def _seed_person(storage, chat_id: str) -> str:
    user_id = f"telegram:test:{chat_id}"
    storage.ensure_user(user_id, source="telegram", metadata={"chat_id": chat_id})
    return user_id


def _kernel(settings, storage):
    auth = AuthorizationService(storage, shared_tenant=LEGACY_OWNER_USER_ID)
    graph = KnowledgeGraph(storage)
    core = ExecutionKernel(auth, settings)
    core.bind_services(storage, graph, WebSurfer(settings), IngestionPipeline(settings, storage, graph))
    return core, auth


def _bodies_by_chat(storage) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for row in storage.list_pending_notifications(limit=100):
        out.setdefault(str(row["chat_id"]), []).append(str(row["body"]))
    return out


@pytest.mark.asyncio
async def test_the_reminder_goes_to_its_author_not_to_the_archive_owner(settings, storage):
    """Мутация: убрать отметку автора у события — тест краснеет.

    Без неё напоминание уходит в чат владельца общего архива, а тот, кто просил,
    не получает ничего.
    """
    shared = _shared_settings(settings)
    storage.ensure_user(LEGACY_OWNER_USER_ID, preset_key="owner", metadata={"chat_id": OWNER_CHAT})
    person = _seed_person(storage, OTHER_CHAT)

    core, auth = _kernel(shared, storage)
    actor = auth.actor_for_user(person, source="telegram")
    assert actor.user_id == LEGACY_OWNER_USER_ID, "общий архив не включился — тест бессмыслен"
    assert actor.own_id == person

    result = await core.execute("remind", {"what": "забрать пропуск", "when": "сегодня"}, actor=actor)
    assert result.success and result.data["created"] is True

    ctx = ServiceContext(settings=shared, storage=storage, kg=KnowledgeGraph(storage), ingestion=None)
    await scan_reminders(ctx)

    by_chat = _bodies_by_chat(storage)
    assert any("пропуск" in body for body in by_chat.get(OTHER_CHAT, [])), (
        "напоминание не дошло до того, кто его попросил"
    )
    assert not any("пропуск" in body for body in by_chat.get(OWNER_CHAT, [])), (
        "личная просьба одного человека ушла в чат другого"
    )


@pytest.mark.asyncio
async def test_an_event_from_a_document_still_reminds_the_archive_owner(settings, storage):
    """Событие без автора — общий материал: напоминает хозяину архива.

    Это вторая половина различения. Если чинить дефект «слать только автору», не
    оставив этой ветки, встречи, извлечённые из документов, перестанут напоминать
    о себе вообще: у них автора нет и не будет.
    """
    shared = _shared_settings(settings)
    storage.ensure_user(LEGACY_OWNER_USER_ID, preset_key="owner", metadata={"chat_id": OWNER_CHAT})
    _seed_person(storage, OTHER_CHAT)

    graph = KnowledgeGraph(storage)
    event = graph.create_entity(LEGACY_OWNER_USER_ID, "Совещание по поверке", EntityType.EVENT)
    graph.set_event_time(
        LEGACY_OWNER_USER_ID,
        event["id"],
        (datetime.now(UTC).date() + timedelta(days=1)).isoformat(),
    )

    ctx = ServiceContext(settings=shared, storage=storage, kg=KnowledgeGraph(storage), ingestion=None)
    await scan_reminders(ctx)

    by_chat = _bodies_by_chat(storage)
    assert any("Совещание" in body for body in by_chat.get(OWNER_CHAT, [])), (
        "событие из документа перестало напоминать о себе"
    )
    assert not any("Совещание" in body for body in by_chat.get(OTHER_CHAT, [])), (
        "общее событие рассылается всем — это спам"
    )


@pytest.mark.asyncio
async def test_without_the_shared_archive_nothing_changes(settings, storage):
    """Обычная настройка: арендатор и человек — одно, поведение прежнее."""
    plain = replace(
        settings,
        shared_archive=False,
        reminders_enabled=True,
        reminders_lead_days=1,
        quiet_hours_start=0,
        quiet_hours_end=0,
    )
    person = _seed_person(storage, OTHER_CHAT)
    auth = AuthorizationService(storage)
    graph = KnowledgeGraph(storage)
    core = ExecutionKernel(auth, plain)
    core.bind_services(storage, graph, WebSurfer(plain), IngestionPipeline(plain, storage, graph))
    actor = auth.actor_for_user(person, source="telegram")

    result = await core.execute("remind", {"what": "позвонить в часть", "when": "сегодня"}, actor=actor)
    assert result.data["created"] is True

    ctx = ServiceContext(settings=plain, storage=storage, kg=KnowledgeGraph(storage), ingestion=None)
    await scan_reminders(ctx)

    by_chat = _bodies_by_chat(storage)
    assert any("позвонить" in body for body in by_chat.get(OTHER_CHAT, []))


@pytest.mark.asyncio
async def test_an_exact_clock_reminder_is_not_queued_early(settings, storage, monkeypatch):
    import friday.organs.reminders as reminders_module

    tuned = replace(
        settings,
        shared_archive=False,
        reminders_enabled=True,
        reminders_lead_days=1,
        quiet_hours_start=0,
        quiet_hours_end=0,
    )
    person = _seed_person(storage, OTHER_CHAT)
    auth = AuthorizationService(storage)
    graph = KnowledgeGraph(storage)
    core = ExecutionKernel(auth, tuned)
    core.bind_services(storage, graph, WebSurfer(tuned), IngestionPipeline(tuned, storage, graph))
    actor = auth.actor_for_user(person, source="telegram")
    result = await core.execute(
        "remind",
        {"what": "точный созвон", "when": "завтра в 15:00"},
        actor=actor,
    )
    assert result.data["created"] is True
    assert result.data["delivery_scheduled"] is True

    planned = datetime.fromisoformat(str(result.data["on"])).date()
    zone = ZoneInfo(tuned.local_timezone or "UTC")
    ctx = ServiceContext(settings=tuned, storage=storage, kg=KnowledgeGraph(storage), ingestion=None)

    monkeypatch.setattr(
        reminders_module,
        "local_now",
        lambda _settings: datetime.combine(planned, time(14, 59), tzinfo=zone),
    )
    await scan_reminders(ctx)
    assert not any("точный созвон" in body for bodies in _bodies_by_chat(storage).values() for body in bodies)

    monkeypatch.setattr(
        reminders_module,
        "local_now",
        lambda _settings: datetime.combine(planned, time(15, 0), tzinfo=zone),
    )
    await scan_reminders(ctx)
    bodies = _bodies_by_chat(storage).get(OTHER_CHAT, [])
    assert sum("точный созвон" in body for body in bodies) == 1
    assert any("15:00" in body for body in bodies)


@pytest.mark.asyncio
async def test_a_date_only_personal_reminder_is_not_queued_a_day_early(
    settings,
    storage,
    monkeypatch,
):
    import friday.organs.reminders as reminders_module

    tuned = replace(
        settings,
        shared_archive=False,
        reminders_enabled=True,
        reminders_lead_days=1,
        quiet_hours_start=0,
        quiet_hours_end=0,
    )
    person = _seed_person(storage, OTHER_CHAT)
    auth = AuthorizationService(storage)
    graph = KnowledgeGraph(storage)
    core = ExecutionKernel(auth, tuned)
    core.bind_services(storage, graph, WebSurfer(tuned), IngestionPipeline(tuned, storage, graph))
    actor = auth.actor_for_user(person, source="telegram")
    result = await core.execute(
        "remind",
        {"what": "отчёт без часа", "when": "завтра"},
        actor=actor,
    )
    assert result.data["created"] is True

    planned = datetime.fromisoformat(str(result.data["on"])).date()
    zone = ZoneInfo(tuned.local_timezone or "UTC")
    ctx = ServiceContext(settings=tuned, storage=storage, kg=KnowledgeGraph(storage), ingestion=None)

    monkeypatch.setattr(
        reminders_module,
        "local_now",
        lambda _settings: datetime.combine(planned - timedelta(days=1), time(23, 59), tzinfo=zone),
    )
    await scan_reminders(ctx)
    assert not any(
        "отчёт без часа" in body for bodies in _bodies_by_chat(storage).values() for body in bodies
    )

    monkeypatch.setattr(
        reminders_module,
        "local_now",
        lambda _settings: datetime.combine(planned, time(0, 0), tzinfo=zone),
    )
    await scan_reminders(ctx)
    bodies = _bodies_by_chat(storage).get(OTHER_CHAT, [])
    assert sum("отчёт без часа" in body for body in bodies) == 1
