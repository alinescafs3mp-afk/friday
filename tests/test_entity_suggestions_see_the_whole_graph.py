"""Совет по сущностям при приёме не должен терять хвост алфавита.

Проход правилом ФИО довёл граф владельца со 110 сущностей до 4458, и вскрылось, что
`_entity_suggestions` берёт `list_entities(limit=2000)`. Список отсортирован по имени,
поэтому 2458 узлов — весь хвост алфавита — переставали существовать для этой проверки
МОЛЧА: новый документ, назвавший такого человека, к его узлу больше не привязывался, а
получал в лучшем случае новое предложение-дубликат.

Обрез был слышен ровно в одном месте — предупреждение в журнале
(«list_entities returned 2000 of 4458»), — и услышан он был случайно.

Второе, что здесь проверяется: быстрый отбор по подстроке, добавленный ради поднятого
потолка, не выбрасывает настоящих совпадений. Он законен потому, что шаблон — тот же
литерал с границами слова и не может совпасть без вхождения подстроки; но обосновать
это надо тестом, а не рассуждением.
"""

from __future__ import annotations

import pytest

from friday.ingestion import IngestionPipeline
from friday.knowledge_graph import KnowledgeGraph
from friday.storage.models import Entity, EntityType, new_id

# Имена НЕ в форме ФИО и без объявляющего слова рядом: правило `explicit_person_patronymic`
# вытащило бы «Ясенев Ярослав Ярославович» прямо из текста, и проба прошла бы на
# обрезанном списке — то есть доказывала бы работу извлекателя, а не поиска по графу.
# Первая редакция этого файла ровно так и ошиблась: мутация `limit=5000` → `2000`
# оставила её зелёной.
TAIL_NAME = "Ядрица Омега Ультра"
HEAD_NAME = "Аметист Ноль Прайм"


@pytest.fixture
def advisor(settings, storage):
    return IngestionPipeline(settings, storage, KnowledgeGraph(storage))


@pytest.fixture
def crowded_graph(storage):
    """Граф, в котором заведомо больше 2000 сущностей и известны оба конца алфавита."""
    storage.ensure_user("alice")
    storage.create_entity(
        Entity(id=new_id("ent"), user_id="alice", name=HEAD_NAME, entity_type=EntityType.PROJECT)
    )
    # Наполнитель между концами: имена начинаются на «Б», то есть сортируются
    # ПОСЛЕ головы и ДО хвоста — иначе проба не про обрез, а про удачу.
    for index in range(2100):
        storage.create_entity(
            Entity(
                id=new_id("ent"),
                user_id="alice",
                name=f"Балласт Узел {index:05d}",
                entity_type=EntityType.PROJECT,
            )
        )
    storage.create_entity(
        Entity(id=new_id("ent"), user_id="alice", name=TAIL_NAME, entity_type=EntityType.PROJECT)
    )
    return storage


def _suggested_names(advisor, content: str) -> set[str]:
    return {str(item.get("name") or "") for item in advisor._entity_suggestions("alice", content)}


def test_an_entity_past_the_two_thousandth_is_still_recognised(crowded_graph, advisor):
    """Имя из конца алфавита обязано находиться так же, как из начала.

    Мутация, которую тест обязан ловить: вернуть `limit=2000` в `_entity_suggestions`.
    Голова остаётся видимой, хвост исчезает — то есть тест, проверяющий только одно
    имя, прошёл бы на сломанном коде. Проверяются ОБА конца.
    """
    text = f"На совещании обсудили {HEAD_NAME} и {TAIL_NAME}, сроки сдвинулись."

    names = _suggested_names(advisor, text)

    assert HEAD_NAME in names, "перестало находиться даже начало алфавита — сломано иное"
    assert TAIL_NAME in names, "хвост алфавита невидим: обрез списка сущностей вернулся"


def test_the_substring_prefilter_keeps_case_insensitive_matches(storage, advisor):
    """Быстрый отбор по подстроке не должен ронять совпадения, которые брал шаблон.

    Шаблон компилируется с `re.I`, поэтому документ вправе назвать сущность в другом
    регистре. Отбор сравнивает `casefold` с `casefold` и обязан такие случаи пропускать
    дальше; иначе поднятый потолок куплен ценой потерянных совпадений.
    """
    storage.ensure_user("alice")
    storage.create_entity(
        Entity(
            id=new_id("ent"),
            user_id="alice",
            name="Проект Альфа",
            entity_type=EntityType.PROJECT,
        )
    )

    names = _suggested_names(advisor, "Сроки по проекту АЛЬФА сдвинулись на май.")

    assert "Проект Альфа" not in names, "проба перестала проверять именно регистр"

    names = _suggested_names(advisor, "Обсудили ПРОЕКТ АЛЬФА и сроки.")

    assert "Проект Альфа" in names, "отбор по подстроке съел совпадение в другом регистре"
