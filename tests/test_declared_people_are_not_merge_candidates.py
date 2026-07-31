"""Два разных объявленных ФИО — два разных человека, и сливать их не предлагают.

Проход правилом ФИО завёл 4349 узлов-людей, и очередь предложений слияния мгновенно
выросла с 20 пар до 45 061. Замер на живой базе: **45 041 из 45 061 (100.0%)** — пары,
где ОБА узла заведены объявляющим правилом; 78% имели уверенность ниже 0.80, а у одной
сущности набралось 173 пары. Такую очередь человек не разберёт никогда, а очередь,
которую невозможно разобрать, — это не осторожность, а отказ системы.

Почему именно запрет, а не поднятие порога. Русские ФИО делят между собой почти всю
структуру, поэтому нечёткое сходство здесь ничего не доказывает; а «улика» вида общих
документов на штатном расписании означает всего лишь «оба в одном списке» — тот же
концентратор, что губит графовый канал.

Цена ошибки несимметрична: два дубликата — неудобство, а два РАЗНЫХ человека, слитые в
один узел, — порча данных, и откатить её нечем: функции разъединения в системе нет.
"""

from __future__ import annotations

import pytest

from jericho.storage.models import Entity, EntityType, new_id

DECLARED = {"extraction_method": "explicit_person_patronymic", "created_by": "ingestion"}
GUESSED = {"extraction_method": "capitalized_person_name", "created_by": "ingestion"}


def _person(storage, name: str, metadata: dict, aliases: list[str] | None = None) -> str:
    entity = Entity(
        id=new_id("ent"),
        user_id="alice",
        name=name,
        entity_type=EntityType.PERSON,
        aliases_json=aliases or [],
        metadata_json=metadata,
    )
    storage.create_entity(entity)
    return entity.id


def _pairs(storage) -> set[frozenset[str]]:
    return {
        frozenset((c.entity_a_id, c.entity_b_id))
        for c in storage.find_duplicate_candidates("alice", min_confidence=0.5)
    }


@pytest.fixture
def alice(storage):
    storage.ensure_user("alice")
    return storage


def test_two_declared_full_names_are_never_proposed_for_merge(alice):
    """Мутация, которую тест обязан ловить: снять условие в `find_duplicate_candidates`
    или заставить `_is_declared_person` возвращать False. Имена нарочно похожи — те же
    фамилия и имя, разные отчества, — так что нечёткое сходство предложит их сразу."""
    left = _person(alice, "Иванов Иван Иванович", DECLARED)
    right = _person(alice, "Иванов Иван Петрович", DECLARED)

    assert frozenset((left, right)) not in _pairs(alice)


def test_a_guessed_name_is_still_proposed(alice):
    """Запрет распространяется только на ОБЪЯВЛЕННЫЕ имена.

    Без этой проверки тест выше прошёл бы и на «выключили дедуп для людей целиком»,
    а это другая правка с другими последствиями.
    """
    left = _person(alice, "Иванов Иван Иванович", GUESSED)
    right = _person(alice, "Иванов Иван Петрович", GUESSED)

    assert frozenset((left, right)) in _pairs(alice)


def test_a_human_written_alias_still_wins(alice):
    """Псевдоним заводит человек, и это его прямое утверждение «это один и тот же».

    Запрет не должен перебивать решение человека — иначе система спорит с владельцем
    там, где он высказался явно.
    """
    left = _person(alice, "Иванов Иван Иванович", DECLARED, aliases=["Иванов И.И."])
    right = _person(alice, "Иванов Иван Петрович", DECLARED, aliases=["Иванов И.И."])

    assert frozenset((left, right)) in _pairs(alice)
