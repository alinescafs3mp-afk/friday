"""Войсковая часть — объявляющее правило, и обе её формы дают ОДИН узел.

Почему правило вообще появилось. Замер `relations = 0` на живом архиве показал не то,
что ожидалось: пар имён в пределах абзаца там 75 728, то есть материала для связей
завались, а кандидатов нынешний словарь («использует», «зависит от», «часть») даёт 70,
и минимум 29 из них — ложный друг: слово «часть» в значении «войсковая часть».

Между двумя именами в этом корпусе стоят не связки, а звания и оружие («рядовой» 9047
раз, «акм» 1596, «сержант» 529): документы списочные, и соседство двух имён в списке —
не отношение. Связывать человека осмысленно не с человеком, а с ЧАСТЬЮ, а её в графе
не было вовсе: 4349 людей против 11 организаций.

Замер перед правкой (стенд `~/.jericho/eval/unit_rule_probe.py`, критерии объявлены до
запуска — не меньше 100 документов и 30 частей, не меньше 200 пар «человек и часть»):
240 документов, 151 различная часть, 67 из них более чем в одном документе, 1079 пар.
Оба критерия перевыполнены.
"""

from __future__ import annotations

import argparse

import pytest

from friday.ingestion import _extract_entities
from friday.ingestion._base import DECLARED_ENTITY_METHODS, normalize_entity_name
from friday.storage.models import EntityType


def _units(text: str) -> list[dict]:
    return [i for i in _extract_entities(text) if i.get("method") == "explicit_military_unit"]


def test_both_declaring_forms_collapse_to_one_node():
    """«в/ч 12345» и «войсковой части 12345» — одна и та же часть.

    Мутация, которую тест обязан ловить: отдать `match.group(0)` вместо канона
    `в/ч {номер}`. Тогда `normalize_entity_name` свернёт формы ПО СЛОВАМ и получит
    `в ч 12345` против `войск част 12345` — два узла на одну часть, и «кто ещё служил
    здесь» ответит половиной. Проверено исполнением до правки.
    """
    short = _units("Проходил службу в в/ч 12345 с 2019 года.")
    long = _units("Прибыл из войсковой части 12345 для дальнейшего прохождения службы.")

    assert len(short) == 1 and len(long) == 1
    assert normalize_entity_name(short[0]["name"]) == normalize_entity_name(long[0]["name"])
    assert short[0]["entity_type"] == EntityType.ORGANIZATION.value
    assert short[0]["confidence"] >= 0.88, "ниже порога автосоздания узел не появится"


def test_the_rule_is_allowed_to_create_a_node_by_itself():
    """Объявляющее правило обязано состоять в белом списке, иначе узел не родится."""
    assert "explicit_military_unit" in DECLARED_ENTITY_METHODS


@pytest.mark.parametrize(
    "text",
    [
        "Часть 12345 имущества передана на склад.",  # «часть» не про войско
        "Стоимость части 99 руб.",  # номер короче четырёх цифр
        "Списано 1234567 рублей по ведомости.",  # число без объявляющего слова
    ],
)
def test_weak_and_bare_forms_are_not_taken(text: str):
    """Слабая форма отброшена сознательно, и это замер, а не осторожность.

    «часть N» дала на живом архиве 48 упоминаний и 4 номера, из которых лишь ОДИН не
    подтверждён однозначной формой. То есть её отсутствие стоит одного узла из 151, а
    её присутствие открывает дорогу «части имущества» и номерам из ведомостей.
    """
    assert _units(text) == []


def test_the_backfill_refuses_a_method_that_may_not_create_nodes():
    """Проход по архиву — не лазейка мимо порога автосоздания.

    Догадка по форме слова (`identifier_syntax`, `capitalized_person_name`) права
    создавать узел не имеет; разовый проход не должен выдавать ей это право оптом.
    """
    from friday.cli import _backfill_entities

    code = _backfill_entities(
        argparse.Namespace(method="identifier_syntax", user=None, batch=10, limit=0, apply=True)
    )

    assert code == 2, "недекларирующий метод обязан быть отвергнут"
