"""Подсветка сущностей в тексте документа.

`evidence_json` хранит имя и метод, но не позиции — и это правильно: текст объекта
можно править, а сохранённое смещение пережило бы правку и указывало бы не туда.
Позиции считаются по запросу из текущего текста.

Опасность здесь была известна заранее и стоила отдельной починки в выдержке:
позиции, найденные в свёрнутой строке, нельзя применять к исходной, если свёртка
меняет длину ('ß' → 'ss', 'ﬁ' → 'fi'). На PDF с лигатурами подсветка молча уехала бы
на соседние слова — выглядя при этом правдоподобно.
"""

from __future__ import annotations

import hashlib

from jericho.mentions import mention_spans
from jericho.storage.models import Entity, EntityType, KnowledgeObject, RawObject, new_id


def _marked(text: str, entities: list[tuple[str, str]]) -> list[str]:
    return [text[m.start : m.end] for m in mention_spans(text, entities)]


def test_a_declined_name_is_highlighted_as_a_whole_word():
    """Имя ищется по основе, поэтому «Иванов» находится в «Иванову» — но выделять
    надо слово целиком: подсветка половины читается как ошибка разметки."""
    text = "Подписал Иванов. Позже Иванову поручено. Иванова не было."
    assert _marked(text, [("Иванов", "e1")]) == ["Иванов", "Иванову", "Иванова"]


def test_ligatures_do_not_shift_the_marks():
    """Ровно тот дефект, что чинили в выдержке: casefold не сохраняет длину, а
    pypdf отдаёт лигатуры как есть."""
    text = "справка ﬁﬁﬁﬁﬁ о деле. Иванов подписал акт."
    spans = mention_spans(text, [("Иванов", "e1")])
    assert [text[s.start : s.end] for s in spans] == ["Иванов"]


def test_yo_is_folded_the_same_way_on_both_sides():
    assert _marked("Пётр Петрович приехал", [("Петр Петрович", "e1")]) == ["Пётр Петрович"]


def test_a_substring_inside_another_word_is_not_a_mention():
    """«нов» внутри «Иванов» — не упоминание сущности «Нов»."""
    assert _marked("Иванов и новость", [("Нов", "e1")]) == []


def test_the_longer_name_wins_so_marks_never_nest():
    text = "Совещание вёл Иван Петров, затем Иван ушёл."
    spans = mention_spans(text, [("Иван", "e1"), ("Иван Петров", "e2")])
    marked = [text[s.start : s.end] for s in spans]
    assert marked == ["Иван Петров", "Иван"]
    assert spans[0].entity_id == "e2" and spans[1].entity_id == "e1"


def test_spans_never_overlap_and_come_in_order():
    text = "Иванов, Петров, Иванов, Сидоров"
    spans = mention_spans(text, [("Иванов", "e1"), ("Петров", "e2"), ("Сидоров", "e3")])
    assert [s.start for s in spans] == sorted(s.start for s in spans)
    for earlier, later in zip(spans, spans[1:], strict=False):
        assert earlier.end <= later.start


def test_a_short_name_is_refused_rather_than_matching_everything():
    """Основа короче трёх знаков совпадает с половиной алфавита."""
    assert mention_spans("Он и он, и снова он", [("Он", "e1")]) == []


def test_empty_inputs_are_safe():
    assert mention_spans("", [("Иванов", "e1")]) == []
    assert mention_spans("текст", []) == []
    assert mention_spans("текст", [("", "e1")]) == []


def test_the_route_marks_only_confirmed_entities(settings, storage):
    """Подсветить предложенное значило бы показать догадку так же, как решение
    человека, — ровно та подмена, которую чинили в легенде источников."""
    from fastapi.testclient import TestClient

    from jericho.knowledge_graph import KnowledgeGraph
    from jericho.permissions import LEGACY_OWNER_USER_ID
    from jericho.server import create_app

    app = create_app(settings)
    with TestClient(app) as client:
        live = app.state.storage
        graph = KnowledgeGraph(live)
        text = "Приказ подписал Иванов. Упомянут также Петров."
        raw = RawObject(
            id=new_id("raw"),
            user_id=LEGACY_OWNER_USER_ID,
            source="test",
            source_ref=new_id("src"),
            raw_content=text,
            content_type="text",
            content_hash=hashlib.sha256(b"mentions").hexdigest(),
        )
        live.store_raw_object(raw)
        knowledge = KnowledgeObject(
            id=new_id("ko"),
            user_id=LEGACY_OWNER_USER_ID,
            raw_object_id=raw.id,
            content=text,
            content_type="text",
            title="Приказ",
        )
        live.store_knowledge_object(knowledge)

        confirmed = Entity(
            id=new_id("ent"), user_id=LEGACY_OWNER_USER_ID, name="Иванов", entity_type=EntityType.PERSON
        )
        proposed = Entity(
            id=new_id("ent"), user_id=LEGACY_OWNER_USER_ID, name="Петров", entity_type=EntityType.PERSON
        )
        live.create_entity(confirmed)
        live.create_entity(proposed)
        graph.link_knowledge_to_entity(
            knowledge.id, confirmed.id, LEGACY_OWNER_USER_ID, status="accepted", reviewed_by="owner"
        )
        graph.link_knowledge_to_entity(knowledge.id, proposed.id, LEGACY_OWNER_USER_ID, status="suggested")

        response = client.get(
            f"/api/admin/knowledge/{knowledge.id}/entity-mentions?user_id={LEGACY_OWNER_USER_ID}",
            headers={"Authorization": f"Bearer {settings.api_token}"},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert [item["name"] for item in body["items"]] == ["Иванов"]
        assert body["truncated"] is False
        marked = text[body["items"][0]["start"] : body["items"][0]["end"]]
        assert marked == "Иванов"


def test_reading_someone_elses_document_is_audited(settings, storage):
    from fastapi.testclient import TestClient

    from jericho.server import create_app

    app = create_app(settings)
    with TestClient(app) as client:
        response = client.get(
            "/api/admin/knowledge/no-such/entity-mentions?user_id=someone",
            headers={"Authorization": f"Bearer {settings.api_token}"},
        )
        assert response.status_code == 404
        actions = {str(row.get("action")) for row in app.state.storage.list_audit_log(limit=20)}
        assert "admin.knowledge.read" in actions, "чужое чтение не записано"
