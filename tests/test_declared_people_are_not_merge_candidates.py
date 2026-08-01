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

from friday.storage.models import Entity, EntityType, new_id

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


def test_the_pair_budget_is_not_spent_on_pairs_that_cannot_qualify(alice):
    """Настоящий кандидат не должен вытесняться парами, которые всё равно отбросят.

    Замерено на живом архиве: набор пар упирался в потолок 214 323 и объявлял список
    неполным, при том что почти все набранные пары ниже отбрасывались правилом «два
    объявленных ФИО». Бюджет тратился на заведомо негодное. После переноса проверки в
    само перечисление обрыв исчез, а кандидатов стало 24 вместо 21 — ровно те три,
    которые вытеснялись.

    Устройство пробы важно. Ключи блокировки перебираются сильнейшими первыми
    (`_KEY_RANK`: variant, token, acronym, short, bigram), а бюджет проверяется МЕЖДУ
    ключами. Поэтому однофамильцы обязаны делить ТОКЕН (ранг 1), а настоящая пара —
    только БИГРАММУ (ранг 4): иначе она попадает в набор тем же ключом, что и они, и
    проба ничего не проверяет. Первая редакция этого теста ошиблась ровно так и
    прошла под мутацией.
    """
    # Сорок объявленных однофамильцев: общий токен «иванов» даёт между собой 780 пар.
    for index in range(40):
        _person(alice, f"Иванов Иван {index:02d}ович", DECLARED)
    # Настоящая пара: общих ТОКЕНОВ с кем-либо нет, только биграммы друг с другом.
    left = _person(alice, "Зюзюкинск", GUESSED)
    right = _person(alice, "Зюзюкинец", GUESSED)

    # Потолок нарочно мал: на живой базе его выедали именно люди.
    found, _ = alice._duplicate_pass("alice", min_confidence=0.5, max_pairs=25)
    pairs = {frozenset((c.entity_a_id, c.entity_b_id)) for c in found}

    assert frozenset((left, right)) in pairs, "настоящую пару вытеснили заведомо негодные"
