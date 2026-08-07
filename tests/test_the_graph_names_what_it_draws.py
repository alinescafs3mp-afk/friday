"""Картина графа называет то, что рисует: цвет, направление, псевдоним.

Три находки разведки, все про читаемость, все подтверждены чтением кода.

* `GRAPH_COLORS` знала восемь видов сущностей, а фильтр предлагал девять:
  `document` рисовался серым «прочим», и включивший фильтр «документ» получал
  узлы, неотличимые от остальных. Легенда при этом строилась из ТРЕТЬЕГО списка
  на семь видов — то есть три источника одной правды расходились попарно.
* Цвет вида связи был у семи из пятнадцати. «Создано», «зависит от», «то же, что»
  рисовались одним серым «связано»: разные утверждения выглядели одинаково.
* Поиск по графу смотрел только в `name`. Сущность, заведённая под официальным
  названием, по своему обиходному псевдониму не находилась, и человек делал
  вывод, что её в графе нет.

Обязательные мутации перечислены в `sol/PROPOSALS.md` #54.
"""

from __future__ import annotations

import re
from pathlib import Path

from friday.storage.models import Entity, EntityType, Relation, RelationType, new_id

APP = Path("friday/admin_ui/static/app.js")
CSS = Path("friday/admin_ui/static/app.css")


def _js_object(name: str) -> dict[str, str]:
    """Прочитать словарь `имя:'#цвет'` из поставляемого `app.js`."""
    source = APP.read_text(encoding="utf-8")
    match = re.search(rf"const {name}=\{{(.*?)\}};", source, re.DOTALL)
    assert match, f"{name} не найден в app.js — проба устарела вместе с кодом"
    return dict(re.findall(r"(\w+):'([^']+)'", match.group(1)))


def _js_list(name: str) -> list[str]:
    source = APP.read_text(encoding="utf-8")
    match = re.search(rf"const {name}=\[(.*?)\];", source, re.DOTALL)
    assert match, f"{name} не найден в app.js"
    return re.findall(r"'([^']+)'", match.group(1))


def test_every_offered_entity_type_has_its_own_colour():
    """Мутация: убрать `document` из палитры — краснеет.

    Фильтр, предлагающий вид, обязан уметь его показать: иначе человек включает
    «документ» и получает узлы, неотличимые от «прочего»."""
    colours = _js_object("GRAPH_COLORS")
    offered = _js_list("GRAPH_TYPES")

    missing = [kind for kind in offered if kind not in colours]
    assert not missing, f"фильтр предлагает виды без своего цвета: {missing}"


def test_every_named_relation_kind_has_its_own_colour():
    """Мутация: вернуть палитру из семи видов — краснеет."""
    colours = _js_object("RELATION_COLORS")
    labels = _js_object("RELATION_LABELS")

    missing = sorted(set(labels) - set(colours))
    assert not missing, f"виды связей, которые система называет, но рисует серым: {missing}"


def test_the_legend_cannot_drift_from_the_palette():
    """Мутация: вернуть легенде собственный список — краснеет.

    Три источника одной правды уже разошлись попарно; теперь источник один."""
    source = APP.read_text(encoding="utf-8")

    assert "GRAPH_TYPES.filter(k=>GRAPH_COLORS[k])" in source, (
        "легенда снова строится из своего списка, а не из палитры"
    )
    labels = _js_object("GRAPH_TYPE_LABELS")
    missing = [kind for kind in _js_list("GRAPH_TYPES") if kind not in labels]
    assert not missing, f"виды без подписи в легенде: {missing}"


def test_every_colour_class_exists_in_the_stylesheet():
    """Цвет в JS без правила в CSS — это серый узел и молчаливая потеря вида."""
    styles = CSS.read_text(encoding="utf-8")

    for kind in _js_object("GRAPH_COLORS"):
        assert f".gfill-{kind}{{" in styles, f"нет заливки для вида «{kind}»"
    for kind in _js_object("RELATION_COLORS"):
        assert f".rl-{kind}{{" in styles, f"нет цвета для вида связи «{kind}»"


def test_only_a_confirmed_relation_carries_an_arrow():
    """Мутация: рисовать стрелку всем рёбрам — краснеет.

    «Иванов руководит отделом» имеет направление, а «встретились в одном
    документе» — нет: стрелка там придумала бы направление, которого не
    наблюдали."""
    source = APP.read_text(encoding="utf-8")

    assert 'marker-end="url(#garrow)"' in source, "стрелок нет вовсе"
    assert "+(rel?' marker-end=\"url(#garrow)\"':'')" in source, (
        "стрелка рисуется не только у подтверждённой связи"
    )
    assert 'id="garrow"' in source, "маркер стрелки не объявлен"


def test_the_graph_search_finds_an_alias(storage):
    """Мутация: искать только по `name` — краснеет.

    Псевдоним — то же имя, и человек ищет им ровно так же.

    Регистр НЕ проверяется намеренно: `LIKE` в SQLite нечувствителен к регистру
    только для ASCII, поэтому «обиходное» не найдёт «Обиходное» — ни по имени,
    ни по псевдониму. Это ограничение существующего поиска по имени, а не
    что-то, внесённое здесь; выдавать его за решённое пробой было бы враньём.

    Узлы картины отбираются по числу связанных документов, поэтому одной
    сущности мало — нужен документ и принятые ссылки. Первая редакция стенда
    этого не делала и получала пустую выдачу при ЛЮБОМ поиске.
    """
    from friday.storage.models import KnowledgeObject, RawObject

    needle = "Обиходное"
    storage.ensure_user("alice")
    entity = Entity(
        id=new_id("ent"),
        user_id="alice",
        name="Официальное название",
        entity_type=EntityType.ORGANIZATION,
        aliases_json=["Обиходное имя"],
    )
    storage.create_entity(entity)
    other = Entity(id=new_id("ent"), user_id="alice", name="Посторонняя", entity_type=EntityType.PERSON)
    storage.create_entity(other)
    storage.create_relation(
        Relation(
            id=new_id("rel"),
            user_id="alice",
            source_entity_id=entity.id,
            target_entity_id=other.id,
            relation_type=RelationType.RELATED_TO,
            weight=0.9,
        )
    )
    raw = RawObject(new_id("raw"), "alice", "test", new_id("ref"), "Документ", "text")
    storage.store_raw_object(raw)
    document = KnowledgeObject(new_id("ko"), "alice", raw.id, content="Документ", title="Документ")
    storage.store_knowledge_object(document)
    for linked in (entity.id, other.id):
        storage.link_knowledge_entity("alice", document.id, linked, status="accepted")
    storage.commit()

    found = storage.graph_overview("alice", search=needle)
    names = {str(node.get("name")) for node in found["nodes"]}

    assert "Официальное название" in names, (
        f"по псевдониму {needle!r} сущность не нашлась — человек решит, что её нет"
    )
