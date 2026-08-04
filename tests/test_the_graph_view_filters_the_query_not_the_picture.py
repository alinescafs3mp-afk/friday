"""Фильтр вида графа сужает ЗАПРОС, а не готовую картинку.

Обзор берёт сто самых связанных сущностей и рисует их. Если фильтр по типу
применять к этой сотне уже в браузере, вид скажет «людей: 3» там, где людей в
архиве четыре тысячи — просто среди ста самых связанных их оказалось трое. Это
тот же класс, что «длина страницы — не факт о корпусе»: свойство запроса выдаётся
за свойство данных.

Поэтому фильтры уходят в SQL, а `total` продолжает считать весь граф.
"""

from __future__ import annotations

from friday.knowledge_graph import KnowledgeGraph
from friday.storage.models import EntityType, KnowledgeObject, RawObject, RelationType, new_id


def _document(storage, user_id: str, text: str, title: str) -> KnowledgeObject:
    raw = RawObject(new_id("raw"), user_id, "test", new_id("ref"), text, "text")
    storage.store_raw_object(raw)
    ko = KnowledgeObject(new_id("ko"), user_id, raw.id, content=text, title=title)
    storage.store_knowledge_object(ko)
    return ko


def _archive(storage) -> dict[str, str]:
    graph = KnowledgeGraph(storage)
    people = {}
    for name in ("Кублик Александр Юрьевич", "Варламова Ольга Васильевна"):
        people[name] = str(graph.create_entity("alice", name, EntityType.PERSON)["id"])
    unit = str(graph.create_entity("alice", "в/ч 30926", EntityType.ORGANIZATION)["id"])
    people["в/ч 30926"] = unit
    town = str(graph.create_entity("alice", "Волжский", EntityType.LOCATION)["id"])
    people["Волжский"] = town
    lonely = str(graph.create_entity("alice", "Одиночка Иван Иванович", EntityType.PERSON)["id"])
    people["Одиночка Иван Иванович"] = lonely

    together = _document(storage, "alice", "Кублик, Варламова и в/ч 30926", "Рапорт")
    for entity_id in (people["Кублик Александр Юрьевич"], people["Варламова Ольга Васильевна"], unit):
        graph.link_knowledge_to_entity(together.id, entity_id, "alice")
    alone = _document(storage, "alice", "Одиночка", "Справка")
    graph.link_knowledge_to_entity(alone.id, lonely, "alice")
    place = _document(storage, "alice", "Волжский", "Адрес")
    graph.link_knowledge_to_entity(place.id, town, "alice")

    graph.create_relation(
        "alice", people["Кублик Александр Юрьевич"], people["Варламова Ольга Васильевна"],
        RelationType.FAMILY_OF, weight=0.9,
    )
    graph.create_relation(
        "alice", people["Кублик Александр Юрьевич"], unit, RelationType.MEMBER_OF, weight=0.8,
    )
    return people


def test_filtering_by_type_still_reports_the_whole_graph_size(storage):
    people = _archive(storage)
    only_people = storage.graph_overview("alice", entity_types=["person"])

    names = {str(node["name"]) for node in only_people["nodes"]}
    assert names == {"Кублик Александр Юрьевич", "Варламова Ольга Васильевна", "Одиночка Иван Иванович"}
    assert only_people["shown"] == 3
    # `total` — свойство архива, а не запроса: сузив вид до людей, человек не
    # перестал иметь в графе организацию и место.
    assert only_people["total"] >= len(people)


def test_only_confirmed_relations_hides_mere_co_occurrence(storage):
    _archive(storage)
    both = storage.graph_overview("alice")
    assert {edge["kind"] for edge in both["edges"]} == {"relation", "cooccurrence"}

    confirmed = storage.graph_overview("alice", only_relations=True)
    assert {edge["kind"] for edge in confirmed["edges"]} == {"relation"}


def test_filtering_by_relation_kind_keeps_only_that_kind(storage):
    _archive(storage)
    kin = storage.graph_overview("alice", only_relations=True, relation_types=["family_of"])
    assert [edge["relation_type"] for edge in kin["edges"]] == ["family_of"]


def test_hiding_isolates_drops_nodes_without_a_single_edge(storage):
    _archive(storage)
    everyone = storage.graph_overview("alice")
    assert any(node["name"] == "Одиночка Иван Иванович" for node in everyone["nodes"])

    connected = storage.graph_overview("alice", hide_isolates=True)
    assert not any(node["name"] == "Одиночка Иван Иванович" for node in connected["nodes"])
    # Подпись обязана согласоваться с картинкой: `shown` считается ПОСЛЕ отсева.
    assert connected["shown"] == len(connected["nodes"])


def test_search_narrows_the_nodes_by_name(storage):
    _archive(storage)
    found = storage.graph_overview("alice", search="Кублик")
    assert [node["name"] for node in found["nodes"]] == ["Кублик Александр Юрьевич"]


def test_a_percent_sign_in_the_search_is_not_a_wildcard(storage):
    # Иначе поиск «%» показывал бы весь граф как совпадение с запросом.
    _archive(storage)
    assert storage.graph_overview("alice", search="%")["nodes"] == []


def test_the_route_passes_filters_through(settings):
    """Маршрут обязан ДОВЕЗТИ фильтр до запроса.

    Проверенное на хранилище правило ничего не стоит, если параметр теряется по
    дороге: вид показал бы всё как раньше, а человек считал бы, что фильтр
    применён.
    """

    from fastapi.testclient import TestClient

    from friday.server import create_app

    app = create_app(settings)
    with TestClient(app) as client:
        owner = {"Authorization": f"Bearer {settings.api_token}"}
        storage = app.state.storage
        people = _archive(storage)
        assert people

        everything = client.get(
            "/api/admin/graph", params={"user_id": "alice"}, headers=owner
        )
        assert everything.status_code == 200
        assert {edge["kind"] for edge in everything.json()["edges"]} == {"relation", "cooccurrence"}

        narrowed = client.get(
            "/api/admin/graph",
            params={"user_id": "alice", "entity_types": "person", "only_relations": "true"},
            headers=owner,
        )
        assert narrowed.status_code == 200
        payload = narrowed.json()
        assert {str(node["entity_type"]) for node in payload["nodes"]} == {"person"}
        assert all(edge.get("kind") == "relation" for edge in payload["edges"])
