"""«За чем я слежу» — это МОИ темы, а не темы всех участников архива.

Найдено ревью уязвимых участков 2026-08-04. Три маршрута слежений звали хранилище
с `actor.user_id`, а в общем архиве это один арендатор на всех. У таблицы
`monitors` столбца автора не было вовсе, то есть различить людей было нечем.

Три вреда одной причиной:

* участник набирает /watching и читает, за какими темами следят остальные. Текст
  запроса — личный интерес: «увольнение такого-то», «проверка по подразделению»;
* он же может СНЯТЬ чужое слежение по идентификатору из этого списка, и владелец
  перестаёт получать то, за чем следил;
* потолок слежений считался на архив, поэтому один участник исчерпывал лимит для
  всех остальных.

Собственная документация маршрута обещала обратное — «Свои мониторы», «Потолок на
человека», — и это тот случай, когда комментарий описывает намерение, а код делает
другое.

Столбец `created_by` пустой у строк, заведённых до правки: автора там негде было
взять. Такие слежения видит только владелец архива — отдавать их участникам по
догадке значило бы ровно ту утечку, ради которой столбец заводится. На живой базе
таких строк ноль, проверено перед миграцией.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def shared(storage):
    storage.ensure_user("tenant")
    return storage


def test_a_participant_sees_only_his_own_topics(shared) -> None:
    """Мутация: снять границу по автору — участник снова читает чужие темы."""
    storage = shared
    storage.create_monitor("tenant", "увольнение Петрова", created_by="person-a")
    storage.create_monitor("tenant", "поставки по договору 214", created_by="person-b")

    mine = storage.list_monitors("tenant", created_by="person-a")

    assert [row["query"] for row in mine] == ["увольнение Петрова"]


def test_a_participant_cannot_stop_another_persons_watch(shared) -> None:
    """Худший исход: владелец молча перестаёт получать то, за чем следил."""
    storage = shared
    theirs = storage.create_monitor("tenant", "поверка приборов", created_by="person-b")

    stopped = storage.stop_monitor(theirs["id"], "tenant", created_by="person-a")

    assert stopped is False
    assert storage.get_monitor(theirs["id"], "tenant", created_by="person-b")["active"] == 1


def test_the_owner_still_sees_everything(shared) -> None:
    """Обратная сторона: надзор владельца остаётся.

    Слишком строгая правка отняла бы у владельца общий обзор — а это не утечка,
    а его собственный архив.
    """
    storage = shared
    storage.create_monitor("tenant", "тема одного", created_by="person-a")
    storage.create_monitor("tenant", "тема другого", created_by="person-b")

    everything = storage.list_monitors("tenant")

    assert len(everything) == 2, "владелец потерял общий обзор слежений"


def test_the_cap_is_per_person(shared) -> None:
    """Потолок считается на ЧЕЛОВЕКА.

    Иначе один участник исчерпывает лимит для всех остальных, и остальные видят
    отказ «слишком много слежений», не заведя ни одного.
    """
    storage = shared
    limit = storage.MAX_ACTIVE_MONITORS
    for number in range(limit):
        storage.create_monitor("tenant", f"тема номер {number}", created_by="person-a")

    with pytest.raises(ValueError):
        storage.create_monitor("tenant", "ещё одна тема", created_by="person-a")

    other = storage.create_monitor("tenant", "тема соседа", created_by="person-b")
    assert other["id"], "лимит одного участника закрыл слежения другому"


def test_the_background_sweep_still_sees_all(shared) -> None:
    """Фоновый обход обязан видеть все живые слежения.

    Он ходит без человека вовсе, и граница по автору тут означала бы, что
    слежения перестают проверяться.
    """
    storage = shared
    storage.create_monitor("tenant", "тема одного", created_by="person-a")
    storage.create_monitor("tenant", "тема другого", created_by="person-b")

    assert len(storage.iter_active_monitors()) == 2


def test_the_routes_pass_the_person() -> None:
    """Проверяется ПОДКЛЮЧЕНИЕ: хранилище умеет границу, если ему дадут человека.

    Тест на хранилище остался бы зелёным при маршрутах, передающих арендатора, —
    ровно так дефект и жил.
    """
    import inspect

    from friday import server

    source = inspect.getsource(server.create_app)
    for anchor in ("list_monitors,", "create_monitor,", "stop_monitor,"):
        at = source.index(anchor)
        call = source[at : at + 320]
        assert "created_by=actor.own_id" in call, f"{anchor} снова идёт без человека"
