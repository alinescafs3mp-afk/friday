"""Общая картина графа обязана отвечать на «как было тогда» и «насколько уверенно».

Дата уже была в обходе окрестности узла (схема 27), а общая картина её не знала:
человек ставил дату, видел окрестность на неё, переключался на весь граф и молча
получал сегодняшнюю картину. Молча — худшая часть, вид выглядел отфильтрованным.

Порог уверенности до этого существовал только в окрестности узла, и то под чужим
именем: панель звала органом «общих документов не меньше» и делила число на 50.
То есть человек двигал число документов, а менял порог уверенности связи.
"""

from __future__ import annotations

import pytest

from friday.storage.models import Entity, EntityType, Relation, RelationType, new_id


def _entity(storage, user_id: str, name: str) -> str:
    entity = Entity(id=new_id("ent"), user_id=user_id, name=name, entity_type=EntityType.PERSON)
    storage.create_entity(entity)
    return entity.id


def _link(storage, user_id: str, entity_id: str, knowledge_id: str) -> None:
    """Узел попадает в общую картину только через документ — так устроен отбор."""

    storage.link_knowledge_entity(
        user_id=user_id, knowledge_object_id=knowledge_id, entity_id=entity_id, status="accepted"
    )


def _relation(storage, user_id: str, source: str, target: str, **fields) -> str:
    relation = Relation(
        id=new_id("rel"),
        user_id=user_id,
        source_entity_id=source,
        target_entity_id=target,
        relation_type=RelationType.MEMBER_OF,
        weight=float(fields.get("weight", 1.0)),
    )
    storage.create_relation(relation)
    updates = {key: value for key, value in fields.items() if key in {"valid_from", "valid_to"}}
    if updates:
        assignments = ", ".join(f"{key}=?" for key in updates)
        with storage.transaction() as conn:
            conn.execute(
                f"UPDATE relations SET {assignments} WHERE id=?",  # noqa: S608 — имена из литералов
                (*updates.values(), relation.id),
            )
    return relation.id


@pytest.fixture
def archive(storage):
    """Двое людей, две связи: одна кончилась в 2023-м, вторая началась в 2024-м."""

    storage.ensure_user("alice", source="test", external_id="alice")
    knowledge_id = new_id("ko")
    from friday.storage.models import KnowledgeObject, RawObject

    raw = RawObject(
        id=new_id("raw"),
        user_id="alice",
        source="test",
        source_ref=new_id("src"),
        raw_content="Приказ",
        content_type="text",
        content_hash="a" * 64,
    )
    storage.store_raw_object(raw)
    storage.store_knowledge_object(
        KnowledgeObject(
            id=knowledge_id,
            user_id="alice",
            raw_object_id=raw.id,
            content="Приказ",
            content_type="text",
            title="Приказ",
        )
    )
    person = _entity(storage, "alice", "Иванов")
    old_unit = _entity(storage, "alice", "в/ч 30926")
    new_unit = _entity(storage, "alice", "в/ч 11111")
    for entity_id in (person, old_unit, new_unit):
        _link(storage, "alice", entity_id, knowledge_id)
    ended = _relation(
        storage, "alice", person, old_unit, weight=0.9, valid_from="2020-01-01", valid_to="2023-06-01"
    )
    current = _relation(storage, "alice", person, new_unit, weight=0.4, valid_from="2024-01-01")
    return {"person": person, "old": old_unit, "new": new_unit, "ended": ended, "current": current}


def _relation_pairs(picture) -> set[tuple[str, str]]:
    return {
        (str(edge["source"]), str(edge["target"]))
        for edge in picture["edges"]
        if edge.get("kind") == "relation"
    }


def test_without_a_date_the_picture_is_todays(storage, archive) -> None:
    """Отменённая связь рядом с действующей читалась бы как одновременная."""

    pairs = _relation_pairs(storage.graph_overview("alice"))
    assert (archive["person"], archive["new"]) in pairs
    assert (archive["person"], archive["old"]) not in pairs


def test_a_date_brings_back_what_was_true_then(storage, archive) -> None:
    pairs = _relation_pairs(storage.graph_overview("alice", as_of="2022-01-01"))
    assert (archive["person"], archive["old"]) in pairs, "связь, верная на ту дату, не показана"
    # Связь, начавшаяся позже, на ту дату ещё не факт.
    assert (archive["person"], archive["new"]) not in pairs


def test_a_date_between_the_two_shows_neither_as_current(storage, archive) -> None:
    pairs = _relation_pairs(storage.graph_overview("alice", as_of="2023-09-01"))
    assert pairs == set(), "между отменой первой и началом второй действующих связей нет"


def test_an_unknown_start_is_not_a_later_start(storage, archive) -> None:
    """Пустой `valid_from` — отсутствие сведений, а не «началось позже»."""

    undated = _relation(storage, "alice", archive["old"], archive["new"], weight=0.8)
    assert undated
    pairs = _relation_pairs(storage.graph_overview("alice", as_of="2021-01-01"))
    assert (archive["old"], archive["new"]) in pairs


def test_the_confidence_floor_drops_the_weak_relation(storage, archive) -> None:
    pairs = _relation_pairs(storage.graph_overview("alice", min_confidence=0.5))
    assert (archive["person"], archive["new"]) not in pairs, "связь весом 0.4 прошла порог 0.5"
    # А на дату, когда была верна сильная связь, порог её не трогает.
    strong = _relation_pairs(storage.graph_overview("alice", as_of="2022-01-01", min_confidence=0.5))
    assert (archive["person"], archive["old"]) in strong


def test_the_floor_and_the_date_work_together(storage, archive) -> None:
    """Порядок применения решает: сначала дата, потом порог — оба сужают отбор."""

    pairs = _relation_pairs(storage.graph_overview("alice", as_of="2022-01-01", min_confidence=0.95))
    assert pairs == set()


def test_the_neighbourhood_takes_the_floor_by_its_own_name(storage, archive) -> None:
    """`min_confidence` в окрестности узла — то же самое, что `min_weight`.

    Имён исторически два, смысл один: вес связи. Тест держит их равными, иначе
    панель, перешедшая на честное имя, молча перестала бы фильтровать.
    """

    by_old_name = storage.get_entity_graph("alice", archive["person"], 1, min_weight=0.5)
    by_new_name = storage.get_entity_graph("alice", archive["person"], 1, min_confidence=0.5)
    assert {edge["id"] for edge in by_old_name["edges"]} == {edge["id"] for edge in by_new_name["edges"]}
    assert archive["current"] not in {edge["id"] for edge in by_new_name["edges"]}


def test_the_route_carries_the_date_and_the_floor(settings) -> None:
    """Правило, проверенное на хранилище, ничего не стоит, если параметр теряется."""

    from fastapi.testclient import TestClient

    from friday.server import create_app

    app = create_app(settings)
    with TestClient(app) as client:
        storage = app.state.storage
        owner = {"Authorization": f"Bearer {settings.api_token}"}
        # Тот же архив, что в фикстуре, но на поднятом приложении.
        storage.ensure_user("alice", source="test", external_id="alice")
        from friday.storage.models import KnowledgeObject, RawObject

        raw = RawObject(
            id=new_id("raw"),
            user_id="alice",
            source="test",
            source_ref=new_id("src"),
            raw_content="Приказ",
            content_type="text",
            content_hash="b" * 64,
        )
        storage.store_raw_object(raw)
        knowledge_id = new_id("ko")
        storage.store_knowledge_object(
            KnowledgeObject(
                id=knowledge_id,
                user_id="alice",
                raw_object_id=raw.id,
                content="Приказ",
                content_type="text",
                title="Приказ",
            )
        )
        person = _entity(storage, "alice", "Иванов")
        unit = _entity(storage, "alice", "в/ч 30926")
        for entity_id in (person, unit):
            _link(storage, "alice", entity_id, knowledge_id)
        _relation(storage, "alice", person, unit, weight=0.9, valid_from="2020-01-01", valid_to="2023-06-01")

        today = client.get("/api/admin/graph", params={"user_id": "alice"}, headers=owner)
        assert today.status_code == 200
        assert not [edge for edge in today.json()["edges"] if edge.get("kind") == "relation"]

        then = client.get(
            "/api/admin/graph", params={"user_id": "alice", "as_of": "2022-01-01"}, headers=owner
        )
        assert then.status_code == 200
        assert [edge for edge in then.json()["edges"] if edge.get("kind") == "relation"], (
            "дата не доехала до запроса — вид показал бы сегодняшнюю картину"
        )

        strict = client.get(
            "/api/admin/graph",
            params={"user_id": "alice", "as_of": "2022-01-01", "min_confidence": 0.95},
            headers=owner,
        )
        assert strict.status_code == 200
        assert not [edge for edge in strict.json()["edges"] if edge.get("kind") == "relation"]
