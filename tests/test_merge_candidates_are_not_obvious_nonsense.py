"""Очередь слияний не должна предлагать очевидно разные объекты.

На боевом корпусе очередь была 45 947 пар, и человеку она показывалась третьей
командой в Telegram (`/merges`). Наверху стояли:

    0.94  Иванов Сергей Александрович ⟷ Сергеев Иван Александрович
    0.91  в/ч 01688 ⟷ в/ч 03079

— разные люди и разные воинские части. Две причины, обе точные.

**Морфология тянет фамилию к имени того же корня.** `normalize_entity_name`
сворачивает «Иванов» → «иван», «Сергеев» → «серг», поэтому у двух РАЗНЫХ людей
получался одинаковый набор токенов, и срабатывало правило «те же слова в другом
порядке» — правило, написанное для одного имени, записанного иначе.

**Номер — это и есть различие.** «в/ч 01688» и «в/ч 03079» совпадают всем, кроме
единственного, что их различает; общая похожесть строк ставила им 0.91. Таких
сущностей в архиве 149.

После обеих правок пересчёт того же корпуса даёт 21 кандидатуру вместо 4842, пар
«в/ч N ⟷ в/ч M» — ноль, а верные («CIDR-ПОДПИСКА» ⟷ «CIDR-ПОДПИСКУ») остаются.
"""

from __future__ import annotations

from friday.storage.models import Entity, EntityType, new_id


def _entity(storage, name: str, kind: EntityType = EntityType.PERSON) -> str:
    created = Entity(id=new_id("ent"), user_id="alice", name=name, entity_type=kind)
    storage.create_entity(created)
    return created.id


def _pairs(storage) -> set[frozenset[str]]:
    found, _ = storage.sweep_entity_duplicates("alice", max_pairs=100_000)
    names = {row["id"]: row["name"] for row in storage.execute("SELECT id, name FROM entities")}
    return {frozenset((names.get(item.entity_a_id, "?"), names.get(item.entity_b_id, "?"))) for item in found}


def test_two_people_whose_surname_matches_the_others_first_name_are_not_a_duplicate(storage):
    """Мутация: сравнивать свёрнутые токены вместо сырых — тест краснеет."""
    storage.ensure_user("alice")
    _entity(storage, "Иванов Сергей Александрович")
    _entity(storage, "Сергеев Иван Александрович")

    pairs = _pairs(storage)
    assert frozenset(("Иванов Сергей Александрович", "Сергеев Иван Александрович")) not in pairs, (
        "предложено слить двух разных людей: морфология свернула «Иванов»→«иван», «Сергеев»→«серг»"
    )


def test_the_same_name_written_in_another_order_is_still_a_duplicate(storage):
    """Обратная сторона: правило существует ради этого случая и должен работать."""
    storage.ensure_user("alice")
    _entity(storage, "Хасанов Руслан Рашитович", EntityType.ORGANIZATION)
    _entity(storage, "Руслан Рашитович Хасанов", EntityType.ORGANIZATION)

    pairs = _pairs(storage)
    assert frozenset(("Хасанов Руслан Рашитович", "Руслан Рашитович Хасанов")) in pairs, (
        "одно и то же имя в другом порядке перестало распознаваться"
    )


def test_units_with_different_numbers_are_never_proposed(storage):
    """Мутация: убрать сверку чисел — тест краснеет.

    Номер — единственное, чем такие названия различаются, и общая похожесть строк
    без этой проверки даёт им 0.91.
    """
    storage.ensure_user("alice")
    _entity(storage, "в/ч 01688", EntityType.ORGANIZATION)
    _entity(storage, "в/ч 03079", EntityType.ORGANIZATION)
    _entity(storage, "в/ч 10216", EntityType.ORGANIZATION)

    pairs = _pairs(storage)
    numbered = [pair for pair in pairs if all("в/ч" in name for name in pair)]
    assert not numbered, f"предложено слить разные воинские части: {numbered}"


def test_a_name_with_a_number_and_the_same_name_without_one_are_still_compared(storage):
    """Правило молчит, когда номер только у одного из двух.

    «Отдел» и «Отдел 5» могут быть одним и тем же — здесь решает не число, а всё
    остальное, и запрещать такую пару заранее нельзя.
    """
    storage.ensure_user("alice")
    left = _entity(storage, "Калининск", EntityType.LOCATION)
    right = _entity(storage, "Калининск 17", EntityType.LOCATION)
    del left, right

    found, _ = storage.sweep_entity_duplicates("alice", max_pairs=100_000)
    assert found, "пара, где номер есть только у одной стороны, отсечена вместе с остальными"


def test_declension_of_the_same_word_is_still_a_duplicate(storage):
    """Самый частый честный случай: одно слово в разных падежах."""
    storage.ensure_user("alice")
    _entity(storage, "CIDR-ПОДПИСКА", EntityType.CONCEPT)
    _entity(storage, "CIDR-ПОДПИСКУ", EntityType.CONCEPT)

    pairs = _pairs(storage)
    assert frozenset(("CIDR-ПОДПИСКА", "CIDR-ПОДПИСКУ")) in pairs, (
        "склонение одного и того же слова перестало распознаваться"
    )
