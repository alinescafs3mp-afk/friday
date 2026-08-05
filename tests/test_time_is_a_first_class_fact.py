"""Время связи — факт, который можно спросить, а не поле в схеме.

Схема 27 завела `valid_from`/`valid_to`/`invalidated_at` ещё 2026-08-04, но на
живом графе замер показал: 192 связи, и у ВСЕХ 192 `valid_from` пуст, отменённых
ноль. Поля были, а спросить «как было в 2024» не мог ни человек, ни агент —
обход всегда шёл по сегодняшней картине.

Три дыры, каждая закрыта здесь:

1. никто не проставлял `valid_from` при принятии связи;
2. `INSERT` при принятии не переносил временные поля ВООБЩЕ — даже
   проставленное значение потерялось бы молча;
3. `as_of` не выходил за пределы хранилища: ни в HTTP, ни у модели.

Смысл `valid_from` назван точно: это не «началось тогда», а «на эту дату уже
было правдой». Рапорт от 15.03.2024 не утверждает, что раньше человек в части
не служил, — он утверждает, что 15 марта служил.
"""

from __future__ import annotations

import pytest

from friday.execution_kernel import ExecutionKernel
from friday.ingestion import IngestionPipeline
from friday.knowledge_graph import KnowledgeGraph
from friday.permissions import ActorContext, AuthorizationService
from friday.storage.models import EntityType, KnowledgeObject, RawObject, RelationType, new_id
from friday.web_surfer import WebSurfer


@pytest.fixture
def kernel_and_actor(settings, storage):
    """Ядро с привязанным графом и владелец как действующее лицо."""

    storage.ensure_user("alice", preset_key="owner")
    auth = AuthorizationService(storage)
    kernel = ExecutionKernel(auth, settings)
    kernel.bind_services(
        storage, KnowledgeGraph(storage), WebSurfer(settings), IngestionPipeline(settings, storage)
    )
    actor = ActorContext(user_id="alice", preset_key="owner", source="test", person_id="alice")
    return kernel, actor, storage


def _document(storage, *, document_date: str) -> KnowledgeObject:
    text = "Рапорт: рядовой Иванов проходит службу в в/ч 30926."
    raw = RawObject(new_id("raw"), "alice", "test", new_id("ref"), text, "text")
    storage.store_raw_object(raw)
    knowledge = KnowledgeObject(
        new_id("ko"),
        "alice",
        raw.id,
        content=text,
        title="Рапорт",
        metadata_json={"document_date": document_date},
    )
    storage.store_knowledge_object(knowledge)
    return knowledge


def _accepted_relation(storage, *, document_date: str) -> tuple[dict, dict, dict]:
    graph = KnowledgeGraph(storage)
    person = graph.create_entity("alice", "Иванов Иван Иванович", EntityType.PERSON)
    unit = graph.create_entity("alice", "в/ч 30926", EntityType.ORGANIZATION)
    knowledge = _document(storage, document_date=document_date)
    candidate = storage.store_relation_candidate(
        "alice",
        str(person["id"]),
        str(unit["id"]),
        "member_of",
        confidence=0.9,
        evidence={"knowledge_object_id": knowledge.id, "excerpt": "проходит службу в в/ч 30926"},
    )
    storage.review_relation_candidate("alice", str(candidate["id"]), "accepted", reviewed_by="человек")
    edges = storage.get_entity_relations(str(person["id"]), "alice")
    assert len(edges) == 1
    return person, unit, edges[0]


def test_an_accepted_relation_carries_the_date_of_the_paper(storage):
    """Дата берётся из документа, а не из дня загрузки.

    Архив загружен разом: `created_at` полутора тысяч документов говорит о дне
    импорта, а не о том, когда это было правдой.
    """

    _person, _unit, edge = _accepted_relation(storage, document_date="2024-03-15")

    assert edge["valid_from"] == "2024-03-15"


def test_a_paper_without_a_date_leaves_the_start_unknown(storage):
    """Пустое начало — это «неизвестно», а не «с начала времён».

    Именно поэтому обход по дате не отбрасывает связи с пустым `valid_from`:
    отсутствие сведений не то же самое, что сведения об отсутствии.
    """

    _person, _unit, edge = _accepted_relation(storage, document_date="")

    assert edge["valid_from"] == ""


def test_the_graph_can_be_asked_how_it_looked_back_then(storage):
    person, _unit, _edge = _accepted_relation(storage, document_date="2024-03-15")
    graph = KnowledgeGraph(storage)

    later = graph.get_entity_graph("alice", str(person["id"]), 1, as_of="2024-06-01")
    earlier = graph.get_entity_graph("alice", str(person["id"]), 1, as_of="2024-01-01")

    assert len(later["edges"]) == 1, "на июнь связь уже подтверждена"
    assert earlier["edges"] == [], "на январь подтверждения ещё не было"
    assert later["as_of"] == "2024-06-01", "дата обязана быть В ОТВЕТЕ: две картины выглядят одинаково"


def test_a_relation_that_ended_still_answers_about_the_past(storage):
    """Отмена — это «было и кончилось», а не «не было».

    Сегодняшняя картина её не показывает, а вопрос про прошлое — показывает.
    """

    person, _unit, edge = _accepted_relation(storage, document_date="2024-03-15")
    graph = KnowledgeGraph(storage)
    graph.invalidate_relation("alice", str(edge["id"]), valid_to="2025-01-10", reason="перевёлся")

    today = graph.get_entity_graph("alice", str(person["id"]), 1)
    back_then = graph.get_entity_graph("alice", str(person["id"]), 1, as_of="2024-06-01")

    assert today["edges"] == []
    assert len(back_then["edges"]) == 1


@pytest.mark.asyncio
async def test_the_model_can_end_a_relation_by_names(kernel_and_actor):
    """«Он перевёлся» должно менять граф, а не только ответ.

    До этого инструмента отмена жила только админским маршрутом: система умела
    согласиться на словах, а связь оставалась действующей.
    """

    kernel, actor, storage = kernel_and_actor
    person, unit, _edge = _accepted_relation(storage, document_date="2024-03-15")

    result = await kernel._relation_end(
        actor=actor,
        source="Иванов Иван Иванович",
        target="в/ч 30926",
        valid_to="2025-01-10",
        reason="перевёлся в другую часть",
    )

    assert result["ended"] is True
    graph = KnowledgeGraph(storage)
    assert graph.get_entity_graph("alice", str(person["id"]), 1)["edges"] == []
    assert len(graph.get_entity_graph("alice", str(person["id"]), 1, as_of="2024-06-01")["edges"]) == 1


@pytest.mark.asyncio
async def test_ending_a_relation_that_does_not_exist_says_so(kernel_and_actor):
    kernel, actor, storage = kernel_and_actor
    graph = KnowledgeGraph(storage)
    graph.create_entity("alice", "Иванов Иван Иванович", EntityType.PERSON)
    graph.create_entity("alice", "в/ч 30926", EntityType.ORGANIZATION)

    result = await kernel._relation_end(actor=actor, source="Иванов Иван Иванович", target="в/ч 30926")

    assert result["ended"] is False
    assert "нет" in result["reason"].lower()


@pytest.mark.asyncio
async def test_ending_an_ambiguous_pair_is_a_noop_until_the_type_is_named(kernel_and_actor):
    kernel, actor, storage = kernel_and_actor
    graph = KnowledgeGraph(storage)
    person = graph.create_entity("alice", "Иванов Иван Иванович", EntityType.PERSON)
    unit = graph.create_entity("alice", "в/ч 30926", EntityType.ORGANIZATION)
    member = graph.create_relation("alice", str(person["id"]), str(unit["id"]), RelationType.MEMBER_OF)
    work = graph.create_relation("alice", str(person["id"]), str(unit["id"]), RelationType.WORKS_ON)

    result = await kernel._relation_end(actor=actor, source="Иванов Иван Иванович", target="в/ч 30926")

    assert result["ended"] is False
    assert result["ambiguous"] is True
    assert [(item["type"], item["id"]) for item in result["candidates"]] == sorted(
        [(RelationType.MEMBER_OF.value, member.id), (RelationType.WORKS_ON.value, work.id)]
    )
    current_ids = {row["id"] for row in storage.get_entity_relations(str(person["id"]), "alice")}
    assert current_ids == {member.id, work.id}, "ambiguity must be resolved before any write"


@pytest.mark.asyncio
async def test_relation_end_type_selects_one_edge_and_is_published_in_the_tool_schema(
    kernel_and_actor,
):
    kernel, actor, storage = kernel_and_actor
    graph = KnowledgeGraph(storage)
    person = graph.create_entity("alice", "Иванов Иван Иванович", EntityType.PERSON)
    unit = graph.create_entity("alice", "в/ч 30926", EntityType.ORGANIZATION)
    member = graph.create_relation("alice", str(person["id"]), str(unit["id"]), RelationType.MEMBER_OF)
    work = graph.create_relation("alice", str(person["id"]), str(unit["id"]), RelationType.WORKS_ON)

    result = await kernel._relation_end(
        actor=actor,
        source="Иванов Иван Иванович",
        target="в/ч 30926",
        relation_type=RelationType.MEMBER_OF.value,
        valid_to="2025-01-10",
    )

    assert result["ended"] is True
    current = storage.get_entity_relations(str(person["id"]), "alice")
    assert [row["id"] for row in current] == [work.id]
    history = {
        row["id"]: row
        for row in storage.get_entity_relations(str(person["id"]), "alice", include_invalidated=True)
    }
    assert history[member.id]["valid_to"] == "2025-01-10"
    enum = kernel.get_tool("relation_end").parameters["properties"]["relation_type"]["enum"]
    assert enum == [item.value for item in RelationType]


@pytest.mark.asyncio
async def test_relation_end_rejects_an_unknown_type_without_writing(kernel_and_actor):
    kernel, actor, storage = kernel_and_actor
    graph = KnowledgeGraph(storage)
    person = graph.create_entity("alice", "Иванов Иван Иванович", EntityType.PERSON)
    unit = graph.create_entity("alice", "в/ч 30926", EntityType.ORGANIZATION)
    member = graph.create_relation("alice", str(person["id"]), str(unit["id"]), RelationType.MEMBER_OF)

    result = await kernel._relation_end(
        actor=actor,
        source="Иванов Иван Иванович",
        target="в/ч 30926",
        relation_type="invented_relation",
    )

    assert result["ended"] is False
    assert result["ambiguous"] is False
    assert "тип" in result["reason"].lower()
    assert [row["id"] for row in storage.get_entity_relations(str(person["id"]), "alice")] == [member.id]


def test_the_backfill_gives_old_relations_the_date_of_their_paper(storage, settings, monkeypatch):
    """Правило действует вперёд — принятые раньше связи остаются без начала.

    Замер на живом графе перед проходом: 192 связи, у ВСЕХ 192 начало пустое.
    Проход нашёл дату для 191; у одной документ своей даты не имеет, и она
    осталась пустой — «неизвестно» это не «с начала времён».
    """

    import argparse

    from friday.cli import _backfill_relation_dates

    person, _unit, edge = _accepted_relation(storage, document_date="2024-03-15")
    storage.execute("UPDATE relations SET valid_from='' WHERE id=?", (str(edge["id"]),))
    storage.commit()
    monkeypatch.setattr("friday.config.load_settings", lambda: settings)
    monkeypatch.setattr("friday.storage.init_storage", lambda _settings: storage)
    monkeypatch.setattr(storage, "close", lambda *args, **kwargs: None)

    assert _backfill_relation_dates(argparse.Namespace(user="alice", apply=True)) == 0

    refreshed = storage.get_entity_relations(str(person["id"]), "alice")
    assert refreshed[0]["valid_from"] == "2024-03-15"


def test_the_local_view_honours_the_filters(storage):
    """Окрестность узла обязана слушать те же фильтры, что и общий вид.

    До правки маршрут `/api/admin/graph/{id}` не принимал НИ ОДНОГО фильтра:
    человек выбирал «только люди», переключался с общей картины на окрестность и
    молча получал всё. Молча — худшая часть: вид выглядел отфильтрованным.
    """

    graph = KnowledgeGraph(storage)
    person = graph.create_entity("alice", "Иванов Иван Иванович", EntityType.PERSON)
    unit = graph.create_entity("alice", "в/ч 30926", EntityType.ORGANIZATION)
    city = graph.create_entity("alice", "Севастополь", EntityType.LOCATION)
    graph.create_relation("alice", str(person["id"]), str(unit["id"]), RelationType.MEMBER_OF)
    graph.create_relation("alice", str(person["id"]), str(city["id"]), RelationType.LOCATED_AT)

    everything = graph.get_entity_graph("alice", str(person["id"]), 1)
    only_units = graph.get_entity_graph("alice", str(person["id"]), 1, entity_types=["organization"])
    only_family = graph.get_entity_graph("alice", str(person["id"]), 1, relation_types=["family_of"])

    assert len(everything["edges"]) == 2
    assert [node["name"] for node in only_units["nodes"] if node["id"] != person["id"]] == ["в/ч 30926"]
    assert len(only_units["edges"]) == 1
    assert only_family["edges"] == [], "родни нет — и рисовать нечего"


def test_a_filtered_out_node_does_not_open_the_next_circle(storage):
    """Отсев сужает ОБХОД, а не картинку.

    Иначе сосед второго круга приезжал бы через связь, которую только что
    выключили: узел отсеян, а путь через него остался.
    """

    graph = KnowledgeGraph(storage)
    person = graph.create_entity("alice", "Иванов Иван Иванович", EntityType.PERSON)
    unit = graph.create_entity("alice", "в/ч 30926", EntityType.ORGANIZATION)
    far = graph.create_entity("alice", "Севастополь", EntityType.LOCATION)
    graph.create_relation("alice", str(person["id"]), str(unit["id"]), RelationType.MEMBER_OF)
    graph.create_relation("alice", str(unit["id"]), str(far["id"]), RelationType.LOCATED_AT)

    filtered = graph.get_entity_graph("alice", str(person["id"]), 2, entity_types=["person"])

    assert [node["id"] for node in filtered["nodes"]] == [str(person["id"])]
    assert filtered["edges"] == []
