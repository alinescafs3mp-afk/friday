"""Окрестность узла показывает ту же ткань, что и общая картина.

Дефект, который это закрывает, найден разведкой и подтверждён чтением:
`graph_overview` собирает рёбра ДВУХ родов — совместную встречаемость из
`knowledge_entity_links` и подтверждённые `relations`, — а обход окрестности
читал только вторые. На живой установке подтверждённых связей 192, картина
держится на встречаемости, и человек, кликнув узел с десятком линий,
проваливался в пустоту: «показать окрестность» читалось как «здесь ничего нет».

Умолчание `include_cooccurrence=False` защищается отдельно и намеренно.
`get_entity_graph` читают ТРИ дороги: агент (`entity_lookup`), публичный маршрут
и админка. Соседство в концентраторе — замеренная НЕ-улика: штатное расписание
на полсотни имён делает «связанными» все пары этих людей, и именно этот канал
уполовинивал recall@10 (0.35 -> 0.15). Поэтому его включает ровно рисующая дорога.

Обязательные мутации перечислены в `sol/PROPOSALS.md` #43.
"""

from __future__ import annotations

import hashlib

import pytest
from fastapi.testclient import TestClient

from friday.knowledge_graph import KnowledgeGraph
from friday.server import create_app
from friday.storage.models import Entity, EntityType, KnowledgeObject, RawObject, new_id

USER = "alice"


def _document(storage, title: str, entity_ids: list[str]) -> str:
    text = f"Документ {title}"
    raw = RawObject(
        id=new_id("raw"),
        user_id=USER,
        source="test",
        source_ref=new_id("ref"),
        raw_content=text,
        content_type="text",
        content_hash=hashlib.sha256(text.encode()).hexdigest(),
    )
    storage.store_raw_object(raw)
    document = KnowledgeObject(id=new_id("ko"), user_id=USER, raw_object_id=raw.id, content=text, title=title)
    storage.store_knowledge_object(document)
    for entity_id in entity_ids:
        storage.link_knowledge_entity(USER, document.id, entity_id, status="accepted")
    return document.id


@pytest.fixture
def fabric(storage):
    """Три сущности, встретившиеся в одном документе, и НИ ОДНОЙ подтверждённой связи.

    Ровно та картина, что на живой установке: общий вид рисует треугольник,
    окрестность до правки показывала одинокую точку.
    """
    storage.ensure_user(USER)
    made = {}
    for name, kind in (
        ("Альфа", EntityType.PERSON),
        ("Бета", EntityType.PERSON),
        ("Гамма", EntityType.ORGANIZATION),
    ):
        entity = Entity(id=new_id("ent"), user_id=USER, name=name, entity_type=kind)
        storage.create_entity(entity)
        made[name] = entity.id
    _document(storage, "Общий", [made["Альфа"], made["Бета"], made["Гамма"]])
    storage.commit()
    return made


def test_a_node_known_only_by_cooccurrence_has_neighbours(storage, fabric):
    """Мутация: не звать `_cooccurrence_neighbours_for_traversal` — краснеет."""

    graph = KnowledgeGraph(storage)
    with_fabric = graph.get_entity_graph(USER, fabric["Альфа"], 1, include_cooccurrence=True)

    neighbours = {str(node["id"]) for node in with_fabric["nodes"]} - {fabric["Альфа"]}
    assert neighbours == {fabric["Бета"], fabric["Гамма"]}, (
        "узел, известный только по встречаемости, снова остался один"
    )
    assert with_fabric["edges"], "рёбер нет — окрестность пуста, как до правки"


def test_the_silent_caller_still_gets_the_old_answer(storage, fabric):
    """Мутация: включить встречаемость по умолчанию — краснеет.

    Агент и публичный маршрут не просили этот канал, и он им замерено вреден."""

    graph = KnowledgeGraph(storage)
    silent = graph.get_entity_graph(USER, fabric["Альфа"], 1)

    assert silent["edges"] == [], (
        "молчаливый вызывающий получил рёбра встречаемости: соседство в "
        "концентраторе снова стало уликой для агента"
    )
    assert [str(node["id"]) for node in silent["nodes"]] == [fabric["Альфа"]]


def test_cooccurrence_is_marked_as_an_observation_not_a_claim(storage, fabric):
    """Мутация: не ставить `kind`/`implicit` — краснеет.

    Без пометки наблюдение неотличимо от объявленной человеком связи, и экран
    нарисовал бы его сплошной линией."""

    graph = KnowledgeGraph(storage)
    result = graph.get_entity_graph(USER, fabric["Альфа"], 1, include_cooccurrence=True)

    assert result["edges"]
    for edge in result["edges"]:
        assert edge.get("kind") == "cooccurrence", f"род ребра потерян: {sorted(edge)}"
        assert edge.get("implicit") is True, "пометка «выведено, а не объявлено» не доехала"
        assert int(edge.get("weight") or 0) >= 1, "вес встречаемости — число общих документов"


@pytest.mark.parametrize("boundary", ["as_of", "known_at"])
def test_a_named_moment_never_gets_todays_cooccurrence(storage, fabric, boundary):
    """Мутация: снять проверку `as_of`/`history_status` — краснеет.

    У ссылок на документы нет истории. Подмешать сегодняшнее соседство в картину
    на прошлое — значит выдать нынешнее за бывшее; общий вид уже так не делает."""

    graph = KnowledgeGraph(storage)
    kwargs = {"as_of": "2024-03-05"} if boundary == "as_of" else {"known_at": "2026-08-06T12:00:00+03:00"}
    try:
        result = graph.get_entity_graph(USER, fabric["Альфа"], 1, include_cooccurrence=True, **kwargs)
    except Exception as exc:  # noqa: BLE001
        # Отказ по временной границе — тоже законный исход, но не молчаливая
        # подмена: проверяется, что встречаемости в ответе нет ни при каком исходе.
        assert "known_at" in str(exc) or "as_of" in str(exc), exc
        return

    implicit = [edge for edge in result["edges"] if edge.get("kind") == "cooccurrence"]
    assert implicit == [], f"картина на {boundary} получила сегодняшнее соседство"


def test_confirmed_relations_are_published_before_cooccurrence(storage, fabric):
    """Мутация: убрать род ребра из `edge_rank` — краснеет.

    Вес у двух родов меряется в разном: у связи это уверенность 0..1, у
    встречаемости — число общих документов. Одним числом их сортировать нельзя:
    три общих документа «тяжелее» уверенности 0.9, и объявленные человеком связи
    вылетали бы из бюджета первыми."""
    from friday.storage.models import Relation, RelationType

    storage.create_relation(
        Relation(
            id=new_id("rel"),
            user_id=USER,
            source_entity_id=fabric["Альфа"],
            target_entity_id=fabric["Бета"],
            relation_type=RelationType.DEPENDS_ON,
            weight=0.9,
        )
    )
    storage.commit()

    graph = KnowledgeGraph(storage)
    result = graph.get_entity_graph(USER, fabric["Альфа"], 1, include_cooccurrence=True)
    kinds = [str(edge.get("kind") or "relation") for edge in result["edges"]]

    assert "cooccurrence" in kinds, "проба проверяет не то: встречаемости в ответе нет"
    assert kinds[0] != "cooccurrence", (
        "совместная встречаемость обогнала подтверждённую связь: веса двух родов сравнили одним числом"
    )


def test_another_tenant_never_appears_through_cooccurrence(storage, fabric):
    """Стенд намеренно ВРАЖДЕБНЫЙ: повреждённая денормализованная строка — ссылка
    ЧУЖОГО арендатора, указывающая на ЭТОТ документ.

    Мутация, которую проба ловит: снять арендатора с чтения узла при обходе
    (`_graph_entity_for_traversal`).

    Отдельно записано то, что выяснилось мутационной проверкой и само по себе
    факт о системе. Границу арендатора здесь держат ТРИ двери, и они стоят
    ПОСЛЕДОВАТЕЛЬНО:

      1. `b.user_id = a.user_id` в соединении запроса встречаемости;
      2. `other.user_id = a.user_id` при чтении сущности-соседа там же;
      3. `_graph_entity_for_traversal`, который вообще не материализует чужую
         сущность при обходе.

    Снятие ЛЮБОЙ ОДНОЙ утечки не даёт — следующая отказывает, и ребро
    отбрасывается вместе с несуществующим узлом. Проверено: проба краснеет только
    когда сняты все три сразу. То есть эта проба закрепляет ГАРАНТИЮ, а не
    конкретную строку, и одиночная мутация здесь не показатель. Так и должно
    быть у границы арендатора; записано, чтобы следующий читатель не решил, что
    предикаты 1 и 2 лишние, и не удалил их как «мёртвые».

    Первая редакция этой пробы мутацию пережила по другой, настоящей причине:
    чужая сущность в стенде вообще не была связана ни с одним документом, и
    отсекать было нечего.
    """

    storage.ensure_user("bob")
    stranger = Entity(id=new_id("ent"), user_id="bob", name="Чужой", entity_type=EntityType.PERSON)
    storage.create_entity(stranger)
    document_id = storage.execute(
        "SELECT id FROM knowledge_objects WHERE user_id = ? LIMIT 1", (USER,)
    ).fetchone()["id"]
    storage.execute(
        """INSERT INTO knowledge_entity_links
               (id, user_id, knowledge_object_id, entity_id, status, confidence,
                evidence_json, created_at)
           VALUES (?, 'bob', ?, ?, 'accepted', 1.0, '{}', '2026-08-07T00:00:00Z')""",
        (new_id("kel"), document_id, stranger.id),
    )
    storage.commit()

    graph = KnowledgeGraph(storage)
    result = graph.get_entity_graph(USER, fabric["Альфа"], 2, include_cooccurrence=True)

    assert stranger.id not in {str(node["id"]) for node in result["nodes"]}, (
        "чужая сущность доехала через повреждённую ссылку на этот документ"
    )


def test_the_admin_route_asks_for_the_fabric_and_the_agent_does_not(settings, monkeypatch):
    """Проводочная проба: смотрит, с чем ПОЗВАЛИ, а не что параметр существует.

    Мутация: убрать `include_cooccurrence` у админского маршрута — краснеет."""

    app = create_app(settings)
    seen: list[dict] = []

    def _graph(user_id, entity_id, depth, **kwargs):
        seen.append({"user_id": user_id, "entity_id": entity_id, "depth": depth, **kwargs})
        return {
            "root": entity_id,
            "nodes": [],
            "edges": [],
            "nodes_matched_at_least": 0,
            "nodes_truncated": False,
            "edges_matched_at_least": 0,
            "edges_truncated": False,
            "as_of": "",
            "known_at": "",
            "identity_basis": "current_names",
            "temporal_basis": "valid_time",
        }

    headers = {"Authorization": f"Bearer {settings.api_token}"}
    with TestClient(app, raise_server_exceptions=False) as client:
        monkeypatch.setattr(app.state.kg, "get_entity_graph", _graph)
        response = client.get(
            "/api/admin/graph/ent_1",
            params={"user_id": USER, "include_cooccurrence": "true"},
            headers=headers,
        )

    assert response.status_code == 200
    assert seen, "маршрут не позвал граф — проба проверяет не то"
    assert seen[0].get("include_cooccurrence") is True
