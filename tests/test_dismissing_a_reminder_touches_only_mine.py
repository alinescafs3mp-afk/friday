"""«Снять» гасит МОЁ напоминание и не трогает чужое.

Найдено ревью уязвимых участков 2026-08-04. В обработчике снятия человек проведён
дважды — `dismiss_notification(actor.own_id, …)` и `reminder_states(actor.own_id,
…)` в списке, — а третий вызов, `silence_reminder`, шёл под АРЕНДАТОРОМ. Признак
есть, но до места решения не доезжает; в общем архиве арендатор один на всех.

Ветка при этом не запасная, а ОСНОВНАЯ: список кладёт в идентификатор пункта
`entity_id` события, кнопка несёт его же, а `dismiss_notification` ищет по
идентификатору СТРОКИ ОЧЕРЕДИ и потому всегда отвечает «не нашла».

Цена ошибки двойная:

* СВОЁ напоминание участника лежит под его `person_id`. UPDATE под арендатором его
  не находит, кладётся посторонняя строка «снято» под арендатором, а собственная
  остаётся ждущей — мост ПОТОМ ДОСТАВИТ напоминание, которое человеку уже
  объявили снятым;
* событие из общего документа орган ставит хозяину архива. Та же вставка занимает
  ключ владельца, и частичный уникальный индекс по `(user_id, dedup_key)` больше
  не даст поставить это напоминание заново. Владелец теряет его молча — ни
  ошибки, ни следа.

Дефект живёт только при общем архиве: в обычной установке `own_id` и `user_id` —
одно и то же. У владельца общий архив включён.
"""

from __future__ import annotations

import pytest

from friday.permissions import ActorContext


def _actor(person: str, tenant: str = "tenant") -> ActorContext:
    return ActorContext(
        user_id=tenant,
        preset_key="user",
        source="test",
        shared_tenant=True,
        person_id=person,
        identity_id=f"identity-of-{person}",
    )


@pytest.fixture
def shared(storage):
    storage.ensure_user("tenant")
    for name in ("person-a", "person-b"):
        storage.ensure_user(name)
    return storage


def _queued(storage, user_id: str, key: str) -> list[dict]:
    return [
        dict(row)
        for row in storage.execute(
            "SELECT user_id, dedup_key, status FROM outbound_notifications WHERE user_id=? AND dedup_key=?",
            (user_id, key),
        ).fetchall()
    ]


def test_silencing_mine_marks_my_own_row(shared) -> None:
    """Мутация: снимать под арендатором — тест краснеет.

    Проверяется постусловие в базе, а не ответ маршрута: маршрут отвечает
    «снято» в обоих случаях, и по нему дефекта не видно.
    """
    storage = shared
    key = "reminder:ent_1:2026-08-05"
    storage.enqueue_notification("person-a", "5001", "Поверка завтра", kind="reminder", dedup_key=key)

    assert storage.silence_reminder("person-a", key, chat_id="5001") is True

    mine = _queued(storage, "person-a", key)
    assert [row["status"] for row in mine] == ["dismissed"]


def test_silencing_does_not_touch_another_persons_row(shared) -> None:
    """Строка соседа остаётся ждущей — иначе он молча теряет напоминание."""
    storage = shared
    key = "reminder:ent_1:2026-08-05"
    storage.enqueue_notification("person-a", "5001", "Поверка завтра", kind="reminder", dedup_key=key)
    storage.enqueue_notification("person-b", "5002", "Поверка завтра", kind="reminder", dedup_key=key)

    storage.silence_reminder("person-a", key, chat_id="5001")

    assert [row["status"] for row in _queued(storage, "person-b", key)] == ["pending"]


def test_silencing_under_the_tenant_would_steal_the_owners_slot(shared) -> None:
    """Почему нельзя гасить под арендатором — показано прямо.

    Это не «ещё один случай», а сам механизм потери: вставка под чужим
    идентификатором занимает ключ, и следующий скан уже не сможет поставить
    напоминание — частичный уникальный индекс не даст.
    """
    storage = shared
    key = "reminder:ent_7:2026-08-06"

    # Так вело себя старое поведение: снятие «от имени арендатора».
    storage.silence_reminder("tenant", key, chat_id="42")
    # А теперь орган пытается поставить напоминание владельцу архива.
    placed = storage.enqueue_notification("tenant", "42", "Совещание", kind="reminder", dedup_key=key)

    assert placed is False, "ключ занят, и это ровно та тихая потеря напоминания"
    assert [row["status"] for row in _queued(storage, "tenant", key)] == ["dismissed"]


def test_the_route_passes_the_person(shared) -> None:
    """Проверяется ПОДКЛЮЧЕНИЕ, а не сам механизм хранилища.

    Хранилище умело гасить по человеку и раньше — ошибка была в том, КОГО ему
    передают. Тест на хранилище остался бы зелёным.
    """
    import inspect

    from friday import server

    source = inspect.getsource(server.create_app)
    at = source.index("storage.silence_reminder(")
    # Окрестность ВЫЗОВА, а не срез от начала функции: первая редакция брала 2600
    # знаков от `def`, и добавленный рядом комментарий вытолкнул вызов за границу.
    # Тест краснел на верном коде — та же хрупкость, что уже ловилась дважды.
    call = source[at : at + 200]

    assert "actor.own_id" in call, "снятие снова идёт под арендатором"
    assert "actor.user_id" not in call
    assert "resolve_chat_id(storage, actor.own_id)" in call
