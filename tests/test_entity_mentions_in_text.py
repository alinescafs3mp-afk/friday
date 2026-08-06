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
import pathlib

import pytest

from friday.mentions import mention_spans
from friday.storage.models import Entity, EntityType, KnowledgeObject, RawObject, new_id


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


def test_an_impossible_long_word_is_rejected_without_walking_its_whole_tail(monkeypatch):
    """The closed suffix table makes work independent of an invalid token's size."""

    import friday.mentions as mention_module

    original = mention_module._is_word_char
    calls = 0

    def counted(character: str) -> bool:
        nonlocal calls
        calls += 1
        return original(character)

    monkeypatch.setattr(mention_module, "_is_word_char", counted)

    assert mention_spans("Иван" + ("а" * 50_000), [("Иван", "person")]) == []
    assert calls < 32, "an already-impossible ending was scanned to the end of the token"


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


def test_the_route_marks_only_confirmed_entities(settings, storage, monkeypatch):
    """Подсветить предложенное значило бы показать догадку так же, как решение
    человека, — ровно та подмена, которую чинили в легенде источников."""
    import asyncio

    from fastapi.testclient import TestClient

    import friday.admin_api._knowledge as knowledge_routes
    from friday.knowledge_graph import KnowledgeGraph
    from friday.permissions import LEGACY_OWNER_USER_ID
    from friday.server import create_app

    original_mentions = knowledge_routes.mention_spans
    ran_off_loop: list[bool] = []

    def guarded_mentions(*args, **kwargs):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            ran_off_loop.append(True)
        else:
            ran_off_loop.append(False)
        return original_mentions(*args, **kwargs)

    monkeypatch.setattr(knowledge_routes, "mention_spans", guarded_mentions)
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
        assert ran_off_loop == [True], "large-document matching ran on the serving event loop"


def test_reading_someone_elses_document_is_audited(settings, storage):
    from fastapi.testclient import TestClient

    from friday.server import create_app

    app = create_app(settings)
    with TestClient(app) as client:
        response = client.get(
            "/api/admin/knowledge/no-such/entity-mentions?user_id=someone",
            headers={"Authorization": f"Bearer {settings.api_token}"},
        )
        assert response.status_code == 404
        actions = {str(row.get("action")) for row in app.state.storage.list_audit_log(limit=20)}
        assert "admin.knowledge.read" in actions, "чужое чтение не записано"


def test_a_verb_from_the_same_root_is_not_a_mention():
    """Правило «не больше трёх знаков» пропускало глаголы.

    «Работа» подсвечивала «работать» (работ+ать), «Победа» — «победили» (побед+или),
    и глагол показывался человеку так же, как подтверждённое им решение. Замена —
    закрытый список дописок, выведенный из замера на живом корпусе.
    """
    assert _marked("Работа встала, надо работать дальше", [("Работа", "e1")]) == ["Работа"]
    assert _marked("Победа близко, мы победили", [("Победа", "e1")]) == ["Победа"]
    # Производное слово — тоже не упоминание: «Москвичи» это не «Москва».
    assert _marked("Москва и Москвичи, поехал в Москву", [("Москва", "e1")]) == [
        "Москва",
        "Москву",
    ]


def test_an_adjective_from_a_place_name_is_a_mention_again():
    """Обратная половина того же дефекта, и она стоила больше.

    Замерено на 600 документах: 10.2% совпадений имеют дописку длиннее трёх знаков,
    и почти всё это законные формы названий — «ского» 771 раз, «ской» 446, «ская»
    369. Прежнее правило их молча выбрасывало.
    """
    assert _marked("Казань, Казанского района, в Казани", [("Казань", "e1")]) == [
        "Казань",
        "Казанского",
        "Казани",
    ]


def test_the_cap_keeps_the_first_mentions_in_the_text_not_the_first_name_parsed():
    """Подпись обещает «первые 500 упоминаний» — значит первые ПО ТЕКСТУ.

    Обрезка стояла внутри разбора имён, отсортированных по длине, поэтому документ,
    где одно имя встречается шестьсот раз, съедал весь запас: вторая подтверждённая
    сущность не получала ни одной подсветки, хотя стояла первым словом документа.
    """
    from friday.mentions import _MAX_SPANS

    text = "Петров подписал. " + ("Иванов. " * (_MAX_SPANS + 100)) + " Петров снова."
    spans = mention_spans(text, [("Петров", "e1"), ("Иванов", "e2")])
    assert len(spans) == _MAX_SPANS
    assert spans[0].name == "Петров", "первое слово документа осталось без подсветки"
    # И порядок ответа — по тексту, а не по именам.
    assert [span.start for span in spans] == sorted(span.start for span in spans)


def test_internal_collection_never_hides_an_earlier_lower_priority_name():
    """The public cap is 500 spans; an internal 5000-candidate wall is not semantics.

    The longer name is resolved first and occurs only after the short standalone
    mention.  The old shared ``found`` list filled with 5000 long-name matches,
    then stopped the literal pass before it ever considered the first word of the
    document.  Sorting that already-truncated pool could not recover the omitted
    winner.

    Mutation: restore ``while len(found) < _COLLECT_LIMIT`` in ``mention_spans``
    (or stop the shared inflected collector at 5000); ``short`` disappears.
    """

    text = "Иван ушёл. " + ("Александр Иванов пришёл. " * 5_001)
    entities = [("Иван", "short"), ("Александр Иванов", "long")]

    spans = mention_spans(text, entities)

    assert len(spans) == 500
    assert spans[0].entity_id == "short"
    assert text[spans[0].start : spans[0].end] == "Иван"
    assert [span.start for span in spans] == sorted(span.start for span in spans)


def test_full_output_page_bounds_lower_priority_literal_rescans(monkeypatch):
    """After top-k is full, duplicate entity cards may inspect only its safe prefix."""

    import friday.mentions as mention_module

    search_ends: list[int | None] = []

    class FindProbe(str):
        def find(self, sub, start=0, end=None):  # noqa: ANN001
            search_ends.append(end)
            if end is None:
                return super().find(sub, start)
            return super().find(sub, start, end)

    monkeypatch.setattr(mention_module, "_MAX_SPANS", 5)
    text = "Иванов. " * 10_000
    folded = FindProbe(text.casefold())
    monkeypatch.setattr(mention_module, "_snippet_fold", lambda _body: folded)

    spans = mention_spans(text, [("Иванов", f"person-{index}") for index in range(50)])

    assert len(spans) == 5
    assert search_ends
    assert sum(end is None or end >= len(text) for end in search_ends) <= 5
    assert search_ends[-1] is not None and search_ends[-1] < len(text) // 10


def test_the_browser_marks_the_same_word_the_server_pointed_at():
    """Стык Python↔браузер: единица измерения меняется вместе с языком.

    Смещения считаются в КОДОВЫХ ТОЧКАХ, а строка в JavaScript адресуется в
    единицах UTF-16. Один эмодзи до упоминания — и разметка уезжает на знак:
    сервер отдавал 17..23, а `body.slice(17,23)` давал « Ивано». Ни один тест на
    стороне Python это поймать не мог: он режет ответ маршрута срезом Python и
    поэтому всегда зелёный. Здесь запускается НАСТОЯЩАЯ функция из app.js.
    """
    import json
    import re
    import shutil
    import subprocess

    node = shutil.which("node")
    if not node:
        pytest.skip("node не установлен — проверить браузерную половину нечем")

    text = "Отчёт 😀 подписал Иванов лично."
    spans = mention_spans(text, [("Иванов", "e1")])
    assert [text[s.start : s.end] for s in spans] == ["Иванов"], "сервер указал не на то слово"

    source = pathlib.Path("friday/admin_ui/static/app.js").read_text(encoding="utf-8")
    body = re.search(r"function highlightMentions\(text,spans\)\{[\s\S]*?\n\}", source)
    assert body, "highlightMentions не найдена в app.js — тест устарел вместе с кодом"
    script = (
        "const esc=v=>String(v??'').replace(/[&<>'\\\"]/g,c=>"
        "({'&':'&amp;','<':'&lt;','>':'&gt;',\"'\":'&#39;','\\\"':'&quot;'}[c]));\n"
        + body.group(0)
        + "\nprocess.stdout.write(highlightMentions("
        + json.dumps(text, ensure_ascii=False)
        + ","
        + json.dumps([{"start": s.start, "end": s.end, "name": s.name} for s in spans])
        + "));"
    )
    rendered = subprocess.run(  # noqa: S603
        [node, "-e", script], capture_output=True, text=True, timeout=30, check=True
    ).stdout
    marked = re.search(r"<mark[^>]*>(.*?)</mark>", rendered)
    assert marked and marked.group(1) == "Иванов", f"браузер подсветил не то слово: {rendered}"
