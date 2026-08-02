"""Сводки — служебные сообщения: адресат один, и материал читается общий.

Заказ владельца 2026-08-02: «все служебные сообщения уходят только мне в
телеграм, другим участникам их слать не надо». Правило применили к диагностике
хоста, но не к двум органам, которые тоже пишут сами: недельной сводке и хронике
«в этот день».

Тотальный аудит нашёл в этом вторую половину: после включения общего архива обе
сводки читали материал по ЛИЧНОМУ идентификатору человека, а материал лежит под
общим арендатором. Для владельца это совпадало, для остальных — сводка строилась
по пустоте. Вместе получалось худшее из двух: чужому участнику уходило служебное
сообщение, и оно же было бессодержательным.

Напоминания служебными не считаются: это просьба самого человека, и она
возвращается ему — см. `test_a_reminder_reaches_the_person_who_asked`.
"""

from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from friday.knowledge_graph import KnowledgeGraph
from friday.organs import ServiceContext, archive_tenant, is_service_recipient, local_now
from friday.organs.chronicle import chronicle_on_this_day
from friday.organs.reflection import reflection_digest
from friday.permissions import LEGACY_OWNER_USER_ID
from friday.storage.models import KnowledgeObject, RawObject, new_id

OWNER_CHAT = "5001"
OTHER_CHAT = "5002"


def _settings(settings):
    return replace(
        settings,
        shared_archive=True,
        chronicle_enabled=True,
        reflection_enabled=True,
        reflection_min_knowledge=1,
        quiet_hours_start=0,
        quiet_hours_end=0,
        # Тип как в разборе настроек: список разрешённых чатов приходит числами.
        telegram_owner_chat_ids=[int(OWNER_CHAT)],
    )


def _seed_person(storage, chat_id: str, *, user_id: str | None = None) -> str:
    uid = user_id or f"telegram:test:{chat_id}"
    storage.ensure_user(uid, source="telegram", metadata={"chat_id": chat_id})
    return uid


def _seed_knowledge(storage, user_id: str, title: str, *, created_at: str | None = None) -> None:
    raw = RawObject(
        id=new_id("raw"),
        user_id=user_id,
        source="test",
        source_ref=new_id("src"),
        raw_content=title,
        content_type="text",
        content_hash=hashlib.sha256(new_id("h").encode()).hexdigest(),
    )
    storage.store_raw_object(raw)
    ko = KnowledgeObject(
        id=new_id("ko"),
        user_id=user_id,
        raw_object_id=raw.id,
        content=title,
        content_type="text",
        title=title,
        summary=title,
        tags_json=["поверка"],
    )
    storage.store_knowledge_object(ko)
    if created_at:
        with storage.transaction() as conn:
            conn.execute("UPDATE knowledge_objects SET created_at=? WHERE id=?", (created_at, ko.id))


def _recipients(storage) -> set[str]:
    return {str(row["chat_id"]) for row in storage.list_pending_notifications(limit=100)}


def test_the_recipient_rule_is_one_for_all_organs(settings):
    """Правило живёт в одном месте, а не по копии на орган."""
    tuned = _settings(settings)
    assert is_service_recipient(tuned, OWNER_CHAT) is True
    assert is_service_recipient(tuned, OTHER_CHAT) is False
    # Пустой список — прежнее поведение: молчать совсем хуже.
    assert is_service_recipient(replace(tuned, telegram_owner_chat_ids=[]), OTHER_CHAT) is True


def test_the_material_is_read_from_the_shared_tenant(settings):
    tuned = _settings(settings)
    assert archive_tenant(tuned, "telegram:test:5002") == LEGACY_OWNER_USER_ID
    assert archive_tenant(replace(tuned, shared_archive=False), "someone") == "someone"


@pytest.mark.asyncio
async def test_the_weekly_digest_reaches_the_owner_alone(settings, storage):
    """Мутация: убрать проверку получателя — тест краснеет."""
    tuned = _settings(settings)
    _seed_person(storage, OWNER_CHAT, user_id=LEGACY_OWNER_USER_ID)
    _seed_person(storage, OTHER_CHAT)
    for index in range(3):
        _seed_knowledge(storage, LEGACY_OWNER_USER_ID, f"запись {index}")

    ctx = ServiceContext(settings=tuned, storage=storage, kg=KnowledgeGraph(storage), ingestion=None)
    await reflection_digest(ctx)

    assert _recipients(storage) <= {OWNER_CHAT}, "недельная сводка ушла не владельцу"


@pytest.mark.asyncio
async def test_on_this_day_reaches_the_owner_alone(settings, storage):
    tuned = _settings(settings)
    _seed_person(storage, OWNER_CHAT, user_id=LEGACY_OWNER_USER_ID)
    _seed_person(storage, OTHER_CHAT)
    # Запись кладётся так, как её кладёт живая система: метка в БАЗЕ — UTC.
    # А день, в который её ищет орган, — МЕСТНЫЙ. Между 21:00 и 24:00 по Москве
    # это разные числа, и первая редакция теста краснела после полуночи на ровном
    # месте: «год назад» считалось от UTC, а «в этот день» — от местной даты.
    #
    # Здесь берётся местная дата (её и спросит орган), а час выбирается заведомо
    # дневной, чтобы перевод в UTC не сдвинул число.
    local_today = local_now(_settings(settings)).date()
    a_year_ago = datetime(
        local_today.year - 1, local_today.month, local_today.day, 12, 0, tzinfo=UTC
    ).isoformat()
    _seed_knowledge(storage, LEGACY_OWNER_USER_ID, "разговор о поверке", created_at=a_year_ago)

    ctx = ServiceContext(settings=tuned, storage=storage, kg=KnowledgeGraph(storage), ingestion=None)
    await chronicle_on_this_day(ctx)

    assert _recipients(storage) <= {OWNER_CHAT}, "хроника дня ушла не владельцу"
    assert _recipients(storage) == {OWNER_CHAT}, "владелец не получил хронику — материал ищется не там"
