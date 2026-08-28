"""Заявка на подтверждение — личная, даже когда архив общий.

ПРОВЕРЕНО на
живом коде до правки: `/api/me/approvals` и `/api/approvals/{id}/decide` брали
`actor.user_id`, а в общем архиве это ОДНА строка на всех. У владельца
`FRIDAY_SHARED_ARCHIVE` включён, то есть дыра была не гипотетической.

Что из этого следовало:

    участник видел чужую заявку в своём списке — вместе с описанием действия;
    участник мог её подтвердить;
    при наличии у него нужного права действие исполнялось бы под НИМ;
    `decided_by` записывался арендатором, то есть авторство решения терялось.

Асимметрия здесь намеренная и в этом вся суть общего архива: документы, граф и
поиск — общие, иначе люди не видели бы материал друг друга. Личными остаются
переписка, напоминания, авторство решений — и заявки, потому что заявка это
просьба КОНКРЕТНОГО человека разрешить действие от его имени.

`requested_by` для этого не годился: туда клали `identity_id`, то есть СПОСОБ
входа (идентификатор токена или связанной телеграм-личности). Человека называет
`own_id`, и разъезжаться по токенам, которыми он входил, заявка не должна.

Чужой идентификатор обязан отвечать ровно тем же, чем несуществующий: иначе
разница ответов сама сообщает, что заявка существует.
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
    """Общий арендатор и два человека в нём."""
    storage.ensure_user("tenant")
    return _actor("person-a"), _actor("person-b")


def _make(storage, actor: ActorContext, tool: str = "purge_user_data") -> dict:
    return storage.create_action_approval(
        actor.user_id,
        tool=tool,
        payload={"target": "всё"},
        summary="Удалить материалы",
        requested_by=actor.own_id,
    )


def test_another_participant_does_not_see_the_request(storage, shared) -> None:
    """Мутация: убрать фильтр по человеку — чужая заявка снова в списке."""
    alice, bob = shared
    _make(storage, alice)

    mine = storage.list_action_approvals(bob.user_id, person_id=bob.own_id)

    assert mine == [], "участник видит чужую заявку вместе с описанием действия"


def test_the_person_who_asked_still_sees_it(storage, shared) -> None:
    """Обратная сторона: правка, прячущая заявку от автора, ломает весь механизм."""
    alice, _ = shared
    created = _make(storage, alice)

    mine = storage.list_action_approvals(alice.user_id, person_id=alice.own_id)

    assert [row["id"] for row in mine] == [created["id"]]


def test_a_stranger_cannot_decide(storage, shared) -> None:
    """Худший исход: чужое действие исполняется под тем, кто его не просил."""
    alice, bob = shared
    created = _make(storage, alice)

    decided = storage.decide_action_approval(
        created["id"], bob.user_id, decision="approve", decided_by=bob.own_id, person_id=bob.own_id
    )

    assert decided is None, "участник подтвердил чужую заявку"
    still = storage.get_action_approval(created["id"], alice.user_id, person_id=alice.own_id)
    assert still["status"] == "pending", "чужое решение изменило состояние заявки"


def test_a_foreign_id_looks_exactly_like_a_missing_one(storage, shared) -> None:
    """Разница ответов сама сообщает, что заявка существует."""
    alice, bob = shared
    created = _make(storage, alice)

    foreign = storage.get_action_approval(created["id"], bob.user_id, person_id=bob.own_id)
    missing = storage.get_action_approval("apr_nonexistent", bob.user_id, person_id=bob.own_id)

    assert foreign is None and missing is None


def test_the_author_can_decide(storage, shared) -> None:
    """Ради чего всё и делается."""
    alice, _ = shared
    created = _make(storage, alice)

    decided = storage.decide_action_approval(
        created["id"],
        alice.user_id,
        decision="approve",
        decided_by=alice.own_id,
        person_id=alice.own_id,
    )

    assert decided is not None and decided["status"] == "approved"
    assert decided["decided_by"] == "person-a", "авторство решения записано арендатором"


def test_without_a_shared_archive_nothing_changes(storage) -> None:
    """Обычная настройка: человек и арендатор — одно и то же, фильтр ничего не меняет.

    Это половина доказательства того, что правка узкая: она не может сломать
    установку с одним пользователем, которых большинство.
    """
    storage.ensure_user("solo")
    actor = ActorContext(user_id="solo", preset_key="owner", source="test")
    created = storage.create_action_approval(
        actor.user_id, tool="purge_user_data", summary="Удалить", requested_by=actor.own_id
    )

    assert [row["id"] for row in storage.list_action_approvals("solo", person_id=actor.own_id)] == [
        created["id"]
    ]
    assert storage.get_action_approval(created["id"], "solo", person_id=actor.own_id) is not None


def test_a_system_request_stays_visible_to_everyone(storage, shared) -> None:
    """Заявку об откате оборвавшегося шага миссии просит не человек, а система.

    Спрятать её от всех было бы ХУЖЕ той дыры, ради которой вводилась личная
    граница: решение по такой заявке просто некому было бы принять, а шаг миссии
    с неизвестным исходом так и остался бы неизвестным.

    Поймано существующими тестами маршрутов сразу после первой редакции правки —
    четыре из них покраснели с 404. Это ровно тот случай, когда «починка одной
    стороны кренит в другую».
    """
    alice, bob = shared
    system = storage.create_action_approval(
        alice.user_id,
        tool="purge_user_data",
        summary="Откатить оборвавшийся шаг",
        requested_by="executive",
    )

    for actor in (alice, bob):
        listed = [row["id"] for row in storage.list_action_approvals(actor.user_id, person_id=actor.own_id)]
        assert system["id"] in listed, "служебная заявка пропала из виду у всех"


def test_the_count_is_personal_too(storage, shared) -> None:
    """Число рядом со списком обязано считать то же, что список показывает."""
    alice, bob = shared
    _make(storage, alice)

    assert storage.count_action_approvals(bob.user_id, person_id=bob.own_id) == 0
    assert storage.count_action_approvals(alice.user_id, person_id=alice.own_id) == 1
