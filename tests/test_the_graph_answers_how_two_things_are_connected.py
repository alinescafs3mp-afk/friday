"""«Как связаны Иванов и Заря» — вопрос про ребро, а не про узел.

На него не отвечает карточка ни одного из объектов: `/profile` показывает
соседей одного узла, а цепочка через промежуточные звенья не видна ни в одной
из двух карточек. В админке путь подсвечивался на картине с самого начала, а в
чате графа не было вовсе, хотя Telegram — первичный интерфейс продукта.

Совместная встречаемость в путь НЕ входит, и это главное решение здесь.
Соседство в концентраторе — замеренная не-улика: штатное расписание на полсотни
имён связывает все пары этих людей, и цепочка через него означала бы «упомянуты
в одном документе», а не «связаны». Молчание об отсутствии пути честнее
выдуманной связи.
"""

from __future__ import annotations

import pytest

from friday.knowledge_graph import KnowledgeGraph
from friday.storage.models import Entity, EntityType, Relation, RelationType, new_id


@pytest.fixture
def graph(storage):
    return KnowledgeGraph(storage)


def _entity(graph: KnowledgeGraph, name: str, kind: EntityType = EntityType.PERSON) -> str:
    entity = graph.storage.create_entity(
        Entity(id=new_id("ent"), user_id="alice", name=name, entity_type=kind)
    )
    return str(entity["id"] if isinstance(entity, dict) else entity.id)


def _relate(graph: KnowledgeGraph, source: str, target: str, kind: RelationType) -> None:
    graph.storage.create_relation(
        Relation(
            id=new_id("rel"),
            user_id="alice",
            source_entity_id=source,
            target_entity_id=target,
            relation_type=kind,
            weight=1.0,
        )
    )


def test_a_chain_through_a_middle_node_is_found(graph):
    """Ровно то, чего не видно ни в одной карточке: связь через посредника."""
    person = _entity(graph, "Иванов")
    department = _entity(graph, "Отдел снабжения", EntityType.ORGANIZATION)
    project = _entity(graph, "Заря", EntityType.PROJECT)
    _relate(graph, person, department, RelationType.MEMBER_OF)
    _relate(graph, department, project, RelationType.RELATED_TO)

    found = graph.find_relation_path("alice", person, project, max_depth=3)

    assert found["found"] is True, found
    assert [step["to"]["name"] for step in found["path"]] == ["Отдел снабжения", "Заря"]
    assert found["path"][0]["from"]["name"] == "Иванов"


def test_the_direction_of_each_claim_is_kept(graph):
    """Путь может идти ПРОТИВ стрелки, и человеку это надо видеть.

    Направление — свойство утверждения («Иванов работает в отделе»), а не обхода.
    Показать обратный шаг как прямой значило бы придумать утверждение.
    """
    person = _entity(graph, "Иванов")
    department = _entity(graph, "Отдел", EntityType.ORGANIZATION)
    _relate(graph, person, department, RelationType.MEMBER_OF)

    forward = graph.find_relation_path("alice", person, department, max_depth=2)
    backward = graph.find_relation_path("alice", department, person, max_depth=2)

    assert forward["path"][0]["forward"] is True
    assert backward["path"][0]["forward"] is False


def test_cooccurrence_is_never_a_step_in_the_path(graph):
    """Главное решение: «упомянуты в одном документе» — не связь.

    Мутация «включить встречаемость в обход» обязана ронять эту пробу: иначе
    концентратор вроде штатного расписания соединит любые два имени, и путь
    перестанет что-либо значить.
    """
    from friday.storage.models import KnowledgeObject, RawObject

    left = _entity(graph, "Петров")
    right = _entity(graph, "Сидоров")
    raw = graph.storage.store_raw_object(
        RawObject(
            id=new_id("raw"),
            user_id="alice",
            content_type="text",
            raw_content="Петров, Сидоров и ещё сорок человек.",
            source="test",
            source_ref="test:hub",
        )
    )
    knowledge = graph.storage.store_knowledge_object(
        KnowledgeObject(
            id=new_id("ko"),
            user_id="alice",
            raw_object_id=str(raw["id"] if isinstance(raw, dict) else raw.id),
            title="Штатное расписание",
            content="Петров, Сидоров и ещё сорок человек.",
        )
    )
    knowledge_id = str(knowledge["id"] if isinstance(knowledge, dict) else knowledge.id)
    for entity_id in (left, right):
        graph.storage.link_knowledge_entity("alice", knowledge_id, entity_id)

    found = graph.find_relation_path("alice", left, right, max_depth=3)

    assert found["found"] is False, "путь прошёл через совместную встречаемость"


def test_an_unreachable_pair_says_how_far_it_looked(graph):
    """Отказ должен быть проверяемым: «в пределах скольких шагов» — часть ответа."""
    left = _entity(graph, "Один")
    right = _entity(graph, "Другой")

    found = graph.find_relation_path("alice", left, right, max_depth=2)

    assert found["found"] is False
    assert found["depth_searched"] == 2


def test_the_command_is_wired_into_the_bridge() -> None:
    """Проба на ПОДКЛЮЧЕНИЕ, а не на механизм: команда без ветки — мёртвый пункт меню."""
    import ast
    import inspect
    import pathlib

    from friday.telegram_bridge import BOT_COMMANDS, TelegramBridge

    assert any(name == "graph" for name, _ in BOT_COMMANDS), "команда не объявлена в меню"

    package = pathlib.Path(inspect.getfile(TelegramBridge)).parent
    dispatched = False
    for path in package.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Compare)
                and isinstance(node.left, ast.Name)
                and node.left.id == "command"
                and any(
                    isinstance(item, ast.Constant) and item.value == "/graph" for item in node.comparators
                )
            ):
                dispatched = True
    assert dispatched, "команда объявлена, но ветки разбора у неё нет"
