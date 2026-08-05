"""Связь кончилась — это не то же самое, что её не было.

У связей в графе было одно время: `created_at`, когда мы о ней узнали. Этого мало
для архива, который живёт годами. Рапорт 2024 года о службе в в/ч 30926 остаётся
фактом о 2024-м после перевода человека — но если единственный способ сказать «уже
не служит» это мягкое удаление, то архив начинает утверждать, что человек там не
служил никогда.

Поэтому у связи два времени, и они разные:

    valid_from / valid_to  — когда это БЫЛО ПРАВДОЙ;
    invalidated_at         — когда МЫ ЭТО ЗАПИСАЛИ.

Второе нужно не для симметрии: только по нему можно ответить, что система считала
верным на прошлой неделе, — а именно этим проверяют, почему она тогда так
ответила.

Пустой `valid_from` — «начало неизвестно», и он НЕ подменяется датой записи:
выдать одно за другое значит объявить, что человек служит в части с того дня,
когда его рапорт попал в архив.
"""

from __future__ import annotations

import pytest

from friday.knowledge_graph import KnowledgeGraph
from friday.storage.models import EntityType, RelationType


def _three(storage) -> tuple[KnowledgeGraph, str, str, str]:
    graph = KnowledgeGraph(storage)
    person = str(graph.create_entity("alice", "Кублик Александр Юрьевич", EntityType.PERSON)["id"])
    first = str(graph.create_entity("alice", "в/ч 30926", EntityType.ORGANIZATION)["id"])
    second = str(graph.create_entity("alice", "в/ч 29544", EntityType.ORGANIZATION)["id"])
    return graph, person, first, second


def test_an_invalidated_relation_leaves_the_current_picture(storage):
    graph, person, unit, _other = _three(storage)
    relation = graph.create_relation("alice", person, unit, RelationType.MEMBER_OF, weight=0.9)

    assert len(storage.get_entity_relations(person, "alice")) == 1
    graph.invalidate_relation("alice", relation.id, valid_to="2026-03-01", reason="перевод")

    # Из текущей картины ушла…
    assert storage.get_entity_relations(person, "alice") == []
    # …но из архива НЕ исчезла: это было и кончилось, а не «не было».
    kept = storage.get_entity_relations(person, "alice", include_invalidated=True)
    assert len(kept) == 1
    assert kept[0]["valid_to"] == "2026-03-01"
    assert kept[0]["invalidated_at"], "не записано, КОГДА мы узнали об окончании"
    assert kept[0]["deleted_at"] is None


def test_the_two_times_are_not_the_same_field(storage):
    """`valid_to` — когда перестало быть правдой, `invalidated_at` — когда узнали."""
    graph, person, unit, _other = _three(storage)
    relation = graph.create_relation("alice", person, unit, RelationType.MEMBER_OF)

    updated = graph.invalidate_relation("alice", relation.id, valid_to="2020-01-01")

    assert updated["valid_to"] == "2020-01-01"
    assert updated["invalidated_at"] > "2020-01-01", (
        "дата записи совпала с датой события — значит одно подменили другим"
    )


def test_asking_how_it_was_then_returns_the_relation_of_that_day(storage):
    graph, person, unit, _other = _three(storage)
    relation = graph.create_relation("alice", person, unit, RelationType.MEMBER_OF)
    graph.invalidate_relation("alice", relation.id, valid_to="2026-03-01")

    # На февраль 2026 связь ещё действовала.
    assert len(storage.get_entity_relations(person, "alice", as_of="2026-02-01")) == 1
    # На май 2026 — уже нет.
    assert storage.get_entity_relations(person, "alice", as_of="2026-05-01") == []


def test_an_unknown_beginning_does_not_hide_the_relation(storage):
    """Пустой `valid_from` — это «неизвестно», а не «началось позже».

    Все связи, созданные до появления временных столбцов, имеют пустое начало.
    Если бы неизвестное начало трактовалось как «ещё не наступило», запрос «как
    было в марте» скрыл бы весь прежний граф целиком.
    """
    graph, person, unit, _other = _three(storage)
    relation = graph.create_relation("alice", person, unit, RelationType.MEMBER_OF)
    assert relation.valid_from == ""

    assert len(storage.get_entity_relations(person, "alice", as_of="2020-01-01")) == 1


def test_a_superseding_relation_must_exist(storage):
    graph, person, unit, other = _three(storage)
    relation = graph.create_relation("alice", person, unit, RelationType.MEMBER_OF)

    with pytest.raises(ValueError, match="замена"):
        graph.invalidate_relation("alice", relation.id, superseded_by="rel_несуществующая")

    replacement = graph.create_relation("alice", person, other, RelationType.MEMBER_OF)
    updated = graph.invalidate_relation(
        "alice", relation.id, superseded_by=replacement.id, valid_to="2026-03-01"
    )
    assert updated["superseded_by"] == replacement.id


def test_invalidating_twice_is_refused(storage):
    """Решение терминально: повторная отмена переписала бы дату задним числом."""
    graph, person, unit, _other = _three(storage)
    relation = graph.create_relation("alice", person, unit, RelationType.MEMBER_OF)
    graph.invalidate_relation("alice", relation.id, valid_to="2026-03-01")

    with pytest.raises(ValueError, match="уже"):
        graph.invalidate_relation("alice", relation.id, valid_to="2026-04-01")


def test_an_ended_relation_can_start_again_without_erasing_history(storage):
    """A repeated real-world interval is history, not a duplicate INSERT."""
    graph, person, unit, _other = _three(storage)
    first = graph.create_relation("alice", person, unit, RelationType.MEMBER_OF, valid_from="2020-01-01")
    graph.invalidate_relation("alice", first.id, valid_to="2023-06-01")

    second = graph.create_relation("alice", person, unit, RelationType.MEMBER_OF, valid_from="2024-01-01")

    assert second.id != first.id
    assert [row["id"] for row in storage.get_entity_relations(person, "alice")] == [second.id]
    assert {row["id"] for row in storage.get_entity_relations(person, "alice", include_invalidated=True)} == {
        first.id,
        second.id,
    }
    assert [row["id"] for row in storage.get_entity_relations(person, "alice", as_of="2022-01-01")] == [
        first.id
    ]
    assert [row["id"] for row in storage.get_entity_relations(person, "alice", as_of="2025-01-01")] == [
        second.id
    ]


def test_creating_the_same_active_interval_remains_idempotent(storage):
    graph, person, unit, _other = _three(storage)
    first = graph.create_relation("alice", person, unit, RelationType.MEMBER_OF, valid_from="2024-01-01")
    repeated = graph.create_relation("alice", person, unit, RelationType.MEMBER_OF, valid_from="2024-01-01")

    assert repeated.id == first.id
    rows = storage.get_entity_relations(person, "alice", include_invalidated=True)
    assert [row["id"] for row in rows] == [first.id]


def test_relation_dates_are_normalized_and_a_backwards_interval_is_refused(storage):
    graph, person, unit, _other = _three(storage)
    relation = graph.create_relation("alice", person, unit, RelationType.MEMBER_OF, valid_from="2024/3/5")
    stored = storage.execute("SELECT valid_from FROM relations WHERE id=?", (relation.id,)).fetchone()
    assert stored["valid_from"] == "2024-03-05"

    with pytest.raises(ValueError, match="valid_to"):
        graph.invalidate_relation("alice", relation.id, valid_to="2024-02-30")
    with pytest.raises(ValueError, match="предшествовать|precede"):
        graph.invalidate_relation("alice", relation.id, valid_to="2024-03-04")

    current = storage.get_entity_relations(person, "alice")
    assert [row["id"] for row in current] == [relation.id], "failed validation mutated the relation"


def test_an_invalid_as_of_is_rejected_instead_of_compared_as_text(storage):
    graph, person, unit, _other = _three(storage)
    graph.create_relation("alice", person, unit, RelationType.MEMBER_OF)

    with pytest.raises(ValueError, match="as_of"):
        storage.get_entity_relations(person, "alice", as_of="not-a-date")


def test_the_overview_does_not_draw_an_ended_relation(storage):
    """Иначе «служит в в/ч А» и «служит в в/ч Б» читаются как одновременные."""
    from friday.storage.models import KnowledgeObject, RawObject, new_id

    graph, person, unit, _other = _three(storage)
    raw = RawObject(new_id("raw"), "alice", "test", new_id("ref"), "Рапорт", "text")
    storage.store_raw_object(raw)
    document = KnowledgeObject(new_id("ko"), "alice", raw.id, content="Рапорт", title="Рапорт")
    storage.store_knowledge_object(document)
    for entity_id in (person, unit):
        graph.link_knowledge_to_entity(document.id, entity_id, "alice")
    relation = graph.create_relation("alice", person, unit, RelationType.MEMBER_OF)

    drawn = storage.graph_overview("alice", only_relations=True)
    assert len(drawn["edges"]) == 1

    graph.invalidate_relation("alice", relation.id, valid_to="2026-03-01")
    assert storage.graph_overview("alice", only_relations=True)["edges"] == []
