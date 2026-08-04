"""Человек, названный в косвенном падеже, — тот же человек.

В служебном документе человека называют не в именительном: «Прошу… Прапорщику
Кублику Александру Юрьевичу», «представить старшего матроса Царегородцева
Андрея Анатольевича». Сопоставление шло по БУКВАЛЬНОМУ вхождению имени, и
замерено на архиве владельца: в рапорте не привязывался ни один из четырёх
военнослужащих, ради которых он написан, — привязывались только их
родственники, названные в именительном («Супруга: Варламова Ольга Васильевна»).

Следствие было не только в потере: субъекта документа не было среди его
сущностей, и извлекатель связей отдавал родство соседнему имени — «Макаров брат
Варламовой» вместо «Макаров брат Кублика».

Дорог, где решается «упомянута ли сущность», три, и починка обязана доехать до
каждой: разбор при приёме, обратный проход по архиву и разметка упоминаний.
"""

from __future__ import annotations

from friday.knowledge_graph import KnowledgeGraph
from friday.mentions import mention_spans
from friday.storage.models import EntityType, KnowledgeObject, RawObject, new_id

_REPORT = (
    "Командиру в/ч 30926\n"
    "Рапорт\n"
    "Прошу Вас предоставить основной отпуск нижепоименованному личному составу:\n"
    "1. Прапорщику Кублику Александру Юрьевичу, Э-465806.\n"
    "Контактные телефоны:\n"
    "Супруга: Варламова Ольга Васильевна: +79992759780\n"
)


def _document(storage, user_id: str, text: str) -> KnowledgeObject:
    raw = RawObject(new_id("raw"), user_id, "test", new_id("ref"), text, "text")
    storage.store_raw_object(raw)
    ko = KnowledgeObject(new_id("ko"), user_id, raw.id, content=text, title="Рапорт")
    storage.store_knowledge_object(ko)
    return ko


def test_ingestion_links_the_subject_named_in_the_dative(storage):
    graph = KnowledgeGraph(storage)
    subject = graph.create_entity("alice", "Кублик Александр Юрьевич", EntityType.PERSON)
    spouse = graph.create_entity("alice", "Варламова Ольга Васильевна", EntityType.PERSON)

    matches = graph.match_mentions("alice", _REPORT)
    found = {str(item["entity_id"]) for item in matches}

    # Супруга названа в именительном и находилась всегда; военнослужащий, ради
    # которого написан рапорт, — только теперь.
    assert str(spouse["id"]) in found
    assert str(subject["id"]) in found
    matched = next(item for item in matches if str(item["entity_id"]) == str(subject["id"]))
    assert matched["matched_text"] == "Кублику Александру Юрьевичу"


def test_the_backfill_pass_links_the_subject_too(storage):
    graph = KnowledgeGraph(storage)
    subject = graph.create_entity("alice", "Царегородцев Андрей Анатольевич", EntityType.PERSON)
    text = (
        "Прошу выплатить мне, старшему матросу Царегородцеву Андрею Анатольевичу, "
        "личный номер Х-764666, денежную компенсацию."
    )
    document = _document(storage, "alice", text)

    storage.backfill_entity_mentions("alice")

    linked = {
        str(link["entity_id"])
        for link in storage.list_knowledge_entity_links(
            "alice", knowledge_object_id=document.id, status="accepted"
        )
    }
    assert str(subject["id"]) in linked


def test_highlighting_covers_the_whole_inflected_name(storage):
    # Разметка обязана покрыть имя ЦЕЛИКОМ: подсветка «Кублику» внутри «Кублику
    # Александру Юрьевичу» читается как ошибка разбора, а не как совпадение.
    spans = mention_spans(_REPORT, [("Кублик Александр Юрьевич", "e1")])
    assert spans
    assert _REPORT[spans[0].start : spans[0].end] == "Кублику Александру Юрьевичу"


def test_a_single_word_name_is_not_folded_onto_a_different_person(storage):
    # «Андрей» и «Андреев» сворачиваются в одну основу «андр». Для однословных
    # имён падежный проход не применяется, иначе разные люди станут одним.
    graph = KnowledgeGraph(storage)
    graph.create_entity("alice", "Андрей", EntityType.PERSON)
    matches = graph.match_mentions("alice", "Приказ подписал Андреев вчера.")
    assert matches == []


def test_an_identifier_with_a_dot_keeps_its_strict_boundaries(storage):
    # Падежный проход не должен ослабить границы у идентификаторов: `BRK` внутри
    # `BRK.A` — другое обозначение, а не упоминание.
    graph = KnowledgeGraph(storage)
    graph.create_entity("alice", "BRK", EntityType.CONCEPT)
    assert graph.match_mentions("alice", "Котировка BRK.A выросла.") == []
