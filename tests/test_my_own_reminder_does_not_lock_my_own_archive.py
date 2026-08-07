"""Собственное личное напоминание не запирает собственный архив (§76).

Найдено живым тестом: реплика «запомни: в четверг в 15:00 совещание по смете»
отвечалась `HTTP 500`. У владельца три напоминания, и они же — приватные сущности
с обиходными именами «совещание», «отчёт в пятницу», «позвонить в автосервис».
Правило §30 объявляло приватным любой текст, повторивший ИМЯ приватной сущности,
поэтому под карантин попадало всё, где эти слова встретились: замерено на копии
живого архива — `108` из `3352` Raw Objects, и каждая новая запись отвергалась.

§30 писался для ОБЩЕГО архива: напоминание одного человека не должно
материализоваться для ДРУГОГО. Внутри личного арендатора самого владельца другого
человека нет — доступ к арендатору уже ограничен изоляцией.
"""

from __future__ import annotations

import pytest

from friday.storage import PrivateMaterialQuarantineError
from friday.storage._privacy import _not_private_raw_dependency
from friday.storage.models import Entity, EntityType, RawObject, new_id

OWNER = "owner-person"
STRANGER = "stranger-tenant"


def _reminder(storage, *, tenant: str, person_id: str, name: str) -> Entity:
    """Сущность-напоминание ровно так, как её пишет инструмент напоминаний."""

    storage.ensure_user(tenant)
    entity = Entity(new_id("ent"), tenant, name, EntityType.EVENT)
    storage.create_entity(entity)
    with storage.transaction() as conn:
        conn.execute(
            """INSERT INTO entity_time(
                   entity_id, user_id, occurred_at, precision, source, updated_at)
               VALUES(?, ?, '2026-08-13T00:00:00Z', 'day', ?, '2026-08-07T00:00:00Z')""",
            (entity.id, tenant, f"reminder:{person_id}"),
        )
        conn.execute(
            """INSERT INTO private_entity_owners(
                   entity_id, person_id, privacy_kind, created_at)
               VALUES(?, ?, 'reminder', '2026-08-07T00:00:00Z')""",
            (entity.id, person_id),
        )
    return entity


def _note(storage, *, tenant: str, text: str) -> RawObject:
    storage.ensure_user(tenant)
    return storage.store_raw_object(
        RawObject(
            new_id("raw"),
            tenant,
            "api",
            f"note:{new_id('src')}",
            text,
            "text",
        )
    )


def _visible(storage, raw_id: str) -> bool:
    row = storage.execute(
        f"""SELECT 1 FROM raw_objects r
             WHERE r.id=? AND {_not_private_raw_dependency("r")}""",  # nosec B608
        (raw_id,),
    ).fetchone()
    return row is not None


def test_a_note_about_my_own_reminder_is_saved_and_stays_visible(storage) -> None:
    _reminder(storage, tenant=OWNER, person_id=OWNER, name="совещание")

    note = _note(storage, tenant=OWNER, text="в четверг совещание по смете, взять папку")

    assert _visible(storage, note.id), "своя же заметка оказалась заперта своим же напоминанием"


def test_a_stranger_tenant_still_cannot_keep_someone_elses_reminder_words(storage) -> None:
    """Исключение — про СВОЙ арендатор. Чужой остаётся под карантином."""

    _reminder(storage, tenant=OWNER, person_id=OWNER, name="совещание")
    storage.ensure_user(STRANGER)

    with pytest.raises(PrivateMaterialQuarantineError):
        _note(storage, tenant=STRANGER, text="в четверг совещание по смете")


def test_a_foreign_person_marker_in_my_tenant_still_quarantines(storage) -> None:
    """Маркер владения указывает на ДРУГОГО человека — исключение не действует."""

    _reminder(storage, tenant=OWNER, person_id="somebody-else", name="совещание")

    with pytest.raises(PrivateMaterialQuarantineError):
        _note(storage, tenant=OWNER, text="в четверг совещание по смете")


def test_the_reminder_itself_stays_hidden_from_generic_reads(storage) -> None:
    """Послабление для носителей не открывает само напоминание."""

    reminder = _reminder(storage, tenant=OWNER, person_id=OWNER, name="совещание")
    _note(storage, tenant=OWNER, text="в четверг совещание по смете")

    hidden = storage.execute(
        "SELECT 1 FROM private_entity_material_cache WHERE entity_id=?",
        (reminder.id,),
    ).fetchone()
    assert hidden is not None, "напоминание перестало быть приватным"
    assert storage.get_entity(OWNER, reminder.id) is None


def test_a_second_reminder_about_the_same_thing_can_be_created(storage) -> None:
    """Именно это делало инструмент напоминаний неработоспособным.

    Новая сущность повторяла имя старой, немедленно становилась приватной,
    `create_entity` отказывал — а человеку при этом отвечалось «записано».
    """

    _reminder(storage, tenant=OWNER, person_id=OWNER, name="совещание")

    second = _reminder(
        storage,
        tenant=OWNER,
        person_id=OWNER,
        name="совещание по смете в четверг",
    )

    assert storage.execute("SELECT 1 FROM entities WHERE id=?", (second.id,)).fetchone() is not None


def test_a_carrier_of_a_foreign_reminder_is_not_freed_by_its_own_one(storage) -> None:
    """Своё напоминание не отпирает текст, который несёт ещё и ЧУЖОЕ имя."""

    _reminder(storage, tenant=OWNER, person_id=OWNER, name="совещание")
    _reminder(storage, tenant=OWNER, person_id="somebody-else", name="аудит вторника")

    with pytest.raises(PrivateMaterialQuarantineError):
        _note(storage, tenant=OWNER, text="совещание и аудит вторника в один день")


# --- Ниже — состояния, в которых условия послабления НЕ дублируют друг друга.
#
# На исправных данных шесть условий совпадают: у своего напоминания сходится и
# арендатор, и маркер владения, и провенанс времени. Из-за этого первая версия
# проб не отличала снятие одного условия от снятия другого — мутация выживала,
# потому что её ловил СОСЕД. Дальше каждое условие проверяется в состоянии, где
# оно единственное, что держит карантин. Такие состояния встречаются: незакрытая
# миграция оставляет напоминание без маркера, а слияние помечает оба конца.


def _reminder_row(
    storage,
    *,
    entity_tenant: str,
    time_tenant: str,
    time_person: str,
    owner_people: tuple[str, ...],
    name: str,
) -> Entity:
    """Напоминание с раздельно заданными арендаторами, временем и владельцами."""

    storage.ensure_user(entity_tenant)
    entity = Entity(new_id("ent"), entity_tenant, name, EntityType.EVENT)
    storage.create_entity(entity)
    with storage.transaction() as conn:
        if time_tenant:
            conn.execute(
                """INSERT INTO entity_time(
                       entity_id, user_id, occurred_at, precision, source, updated_at)
                   VALUES(?, ?, '2026-08-13T00:00:00Z', 'day', ?, '2026-08-07T00:00:00Z')""",
                (entity.id, time_tenant, f"reminder:{time_person}"),
            )
        for person in owner_people:
            conn.execute(
                """INSERT INTO private_entity_owners(
                       entity_id, person_id, privacy_kind, created_at)
                   VALUES(?, ?, 'reminder', '2026-08-07T00:00:00Z')""",
                (entity.id, person),
            )
    return entity


def test_a_reminder_living_in_another_tenant_keeps_the_quarantine(storage) -> None:
    """Всё сходится, кроме арендатора САМОЙ сущности — карантин обязан держать."""

    storage.ensure_user(OWNER)
    _reminder_row(
        storage,
        entity_tenant=STRANGER,
        time_tenant=OWNER,
        time_person=OWNER,
        owner_people=(OWNER,),
        name="совещание",
    )

    with pytest.raises(PrivateMaterialQuarantineError):
        _note(storage, tenant=OWNER, text="в четверг совещание по смете")


def test_a_reminder_without_an_owner_marker_keeps_the_quarantine(storage) -> None:
    """Приватность есть (провенанс времени), а долговечного владельца нет."""

    _reminder_row(
        storage,
        entity_tenant=OWNER,
        time_tenant=OWNER,
        time_person=OWNER,
        owner_people=(),
        name="совещание",
    )

    with pytest.raises(PrivateMaterialQuarantineError):
        _note(storage, tenant=OWNER, text="в четверг совещание по смете")


def test_a_reminder_without_a_schedule_keeps_the_quarantine(storage) -> None:
    """Владелец есть, расписания нет.

    Состояние встречается: слияние до изоляции удаляло строку времени исходной
    сущности, свернув её имя в алиасы цели, и стартовая миграция ставит таким
    сущностям маркер владения без провенанса времени.
    """

    _reminder_row(
        storage,
        entity_tenant=OWNER,
        time_tenant="",
        time_person="",
        owner_people=(OWNER,),
        name="совещание",
    )

    with pytest.raises(PrivateMaterialQuarantineError):
        _note(storage, tenant=OWNER, text="в четверг совещание по смете")


def test_a_marker_naming_another_person_keeps_the_quarantine(storage) -> None:
    """Провенанс времени свой, а долговечный владелец — другой человек."""

    _reminder_row(
        storage,
        entity_tenant=OWNER,
        time_tenant=OWNER,
        time_person=OWNER,
        owner_people=("somebody-else",),
        name="совещание",
    )

    with pytest.raises(PrivateMaterialQuarantineError):
        _note(storage, tenant=OWNER, text="в четверг совещание по смете")


def test_a_reminder_scheduled_for_another_person_keeps_the_quarantine(storage) -> None:
    """Маркер владения свой, а расписание заведено на другого человека."""

    _reminder_row(
        storage,
        entity_tenant=OWNER,
        time_tenant=OWNER,
        time_person="somebody-else",
        owner_people=(OWNER,),
        name="совещание",
    )

    with pytest.raises(PrivateMaterialQuarantineError):
        _note(storage, tenant=OWNER, text="в четверг совещание по смете")


def test_a_reminder_timed_in_another_tenant_keeps_the_quarantine(storage) -> None:
    """Строка времени принадлежит чужому арендатору."""

    storage.ensure_user(STRANGER)
    _reminder_row(
        storage,
        entity_tenant=OWNER,
        time_tenant=STRANGER,
        time_person=OWNER,
        owner_people=(OWNER,),
        name="совещание",
    )

    with pytest.raises(PrivateMaterialQuarantineError):
        _note(storage, tenant=OWNER, text="в четверг совещание по смете")
