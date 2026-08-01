"""Сопоставление сущностей не строится на полной выборке list_entities.

Задача #50: после прохода ФИО граф владельца вырос до 4458 узлов при потолке
`list_entities` 5000. Стена молчаливая — ORDER BY name, отрезается хвост алфавита,
и сущность из него исчезает для match_mentions / search_entities /
_entity_suggestions. Поднятие потолка — отсрочка.

Лечение: кандидаты из текста → keyed lookup. Тест обязан:
1. находить оба конца алфавита при графе ЗАВЕДОМО больше 5000;
2. не звать list_entities на путях точного упоминания (мутация: вернуть старый
   обход — тест красный);
3. сохранять многословные имена без объявляющего слова (ловушка наивного
   «искать по словам»).
"""

from __future__ import annotations

import pytest

from friday.entity_phrases import mention_phrase_candidates
from friday.ingestion import IngestionPipeline
from friday.knowledge_graph import KnowledgeGraph
from friday.storage.models import Entity, EntityType, new_id

TAIL_NAME = "Ядрица Омега Ультра"
HEAD_NAME = "Аметист Ноль Прайм"
ALIAS_HOST = "Северный Узел"
ALIAS_VALUE = "СевУзел-9"


@pytest.fixture
def graph(storage):
    return KnowledgeGraph(storage)


@pytest.fixture
def advisor(settings, storage, graph):
    return IngestionPipeline(settings, storage, graph)


def _fill_past_ceiling(storage, *, count: int = 5100) -> None:
    """Enough entities that list_entities(limit=5000) would drop the alphabetical tail."""
    storage.ensure_user("alice")
    storage.create_entity(
        Entity(id=new_id("ent"), user_id="alice", name=HEAD_NAME, entity_type=EntityType.PROJECT)
    )
    for index in range(count):
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
    storage.create_entity(
        Entity(
            id=new_id("ent"),
            user_id="alice",
            name=ALIAS_HOST,
            entity_type=EntityType.PROJECT,
            aliases_json=[ALIAS_VALUE],
        )
    )


def test_phrase_candidates_keep_multiword_names():
    """Без n-грамм инверсия теряет ФИО и длинные названия проектов."""
    text = f"На совещании обсудили {HEAD_NAME} и {TAIL_NAME}."
    phrases = mention_phrase_candidates(text)
    assert HEAD_NAME in phrases
    assert TAIL_NAME in phrases
    assert "Ядрица Омега" in phrases


def test_match_mentions_sees_both_alphabet_ends_past_list_cap(storage, graph):
    _fill_past_ceiling(storage)
    assert storage.count_entities("alice") > 5000

    text = f"Обсудили {HEAD_NAME} вместе с {TAIL_NAME}."
    names = {item["name"] for item in graph.match_mentions("alice", text)}

    assert HEAD_NAME in names, "голова алфавита пропала — сломано иное"
    assert TAIL_NAME in names, "хвост алфавита невидим: сопоставление снова на полной выборке"


def test_search_entities_sees_tail_past_list_cap(storage, graph):
    _fill_past_ceiling(storage)
    found = {item["name"] for item in graph.search_entities("alice", TAIL_NAME, limit=5)}
    assert TAIL_NAME in found


def test_entity_suggestions_do_not_call_list_entities(storage, advisor, monkeypatch):
    """Мутация: вернуть `list_entities(limit=5000)` в _entity_suggestions.

    Имена не в форме ФИО и без объявляющего слова — извлекатель их не даст, проба
    ловит именно поиск по графу. list_entities на этом пути больше не нужен.
    """
    storage.ensure_user("alice")
    storage.create_entity(
        Entity(id=new_id("ent"), user_id="alice", name=TAIL_NAME, entity_type=EntityType.PROJECT)
    )
    storage.create_entity(
        Entity(id=new_id("ent"), user_id="alice", name=HEAD_NAME, entity_type=EntityType.PROJECT)
    )

    def boom(*_args, **_kwargs):
        raise AssertionError("list_entities must not back exact-mention suggestions")

    monkeypatch.setattr(storage, "list_entities", boom)

    text = f"На совещании обсудили {HEAD_NAME} и {TAIL_NAME}, сроки сдвинулись."
    names = {str(item.get("name") or "") for item in advisor._entity_suggestions("alice", text)}
    assert HEAD_NAME in names
    assert TAIL_NAME in names


def test_match_mentions_do_not_call_list_entities(storage, graph, monkeypatch):
    storage.ensure_user("alice")
    storage.create_entity(
        Entity(id=new_id("ent"), user_id="alice", name=TAIL_NAME, entity_type=EntityType.PROJECT)
    )

    def boom(*_args, **_kwargs):
        raise AssertionError("match_mentions must not walk list_entities")

    monkeypatch.setattr(storage, "list_entities", boom)

    hits = graph.match_mentions("alice", f"Документ про {TAIL_NAME}.")
    assert any(item["name"] == TAIL_NAME for item in hits)


def test_alias_mention_is_found_without_listing_cap(storage, graph):
    _fill_past_ceiling(storage)
    hits = graph.match_mentions("alice", f"Код узла {ALIAS_VALUE} в отчёте.")
    assert any(item["name"] == ALIAS_HOST for item in hits)


def test_find_entities_by_normalized_names_is_keyed(storage):
    storage.ensure_user("alice")
    storage.create_entity(
        Entity(id=new_id("ent"), user_id="alice", name="Проект Альфа", entity_type=EntityType.PROJECT)
    )
    found = storage.find_entities_by_normalized_names("alice", ["проект альфа", "нет такого"])
    assert len(found) == 1
    assert found[0]["name"] == "Проект Альфа"


def test_iter_entities_walks_past_five_thousand(storage):
    storage.ensure_user("alice")
    for index in range(12):
        storage.create_entity(
            Entity(
                id=new_id("ent"),
                user_id="alice",
                name=f"Узел {index:02d}",
                entity_type=EntityType.CONCEPT,
            )
        )
    walked = list(storage.iter_entities("alice", page_size=5))
    assert len(walked) == 12


def test_mutation_empty_keyed_lookup_blinds_match_mentions(storage, graph, monkeypatch):
    """Мутация защищаемого кода: пустой keyed lookup → упоминаний нет.

    Если тест зелёный при такой подмене, он не смотрит на инвертированный путь.
    """
    storage.ensure_user("alice")
    storage.create_entity(
        Entity(id=new_id("ent"), user_id="alice", name=TAIL_NAME, entity_type=EntityType.PROJECT)
    )
    monkeypatch.setattr(storage, "find_entities_by_normalized_names", lambda *a, **k: [])
    assert graph.match_mentions("alice", f"Документ про {TAIL_NAME}.") == []
