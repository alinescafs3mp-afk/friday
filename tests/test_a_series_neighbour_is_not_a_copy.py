"""Соседний документ серии — не копия, и порогом их не различить.

Это записано в шапке `friday/dedup.py` с самого её написания: «серия текстуально
почти идентична по построению». Замер на живом архиве показал, что порог 0.928 от
этого не спасает — в очереди висели 144 пары со сходством 0.99–1.00, и среди них:

    «13 день.docx» ⟷ «12 день.docx»   — разные дни одного курса
    «ЖП.pdf» ⟷ «ЖП1.pdf»              — журнал за 25 и за 24 августа
    «строевка 05.03» ⟷ «строевка 07.03» — разные дни

Слить их значило бы стереть данные. Различающий признак есть, и он не в тексте:
у соседей по серии различается собственная дата документа или числа в имени.
Правило отсеивает 69 пар из 144.

Порядок проверок важен, и это тоже замерено. Все 55 пар, закрытых проходом точных
копий, имеют текст знак в знак — и среди них есть «12 день» ⟷ «13 день», то есть
один файл, дважды сохранённый под разными именами. Правило, поставленное ДО
проверки на точное совпадение, забраковало бы 18 верных решений; поставленное
после — не трогает ни одного.
"""

from __future__ import annotations

import hashlib

from friday.dedup import series_neighbour_reason
from friday.storage.models import KnowledgeObject, RawObject, new_id


def _store(storage, *, text: str, title: str, document_date: str = "") -> str:
    raw = RawObject(
        id=new_id("raw"),
        user_id="alice",
        source="test",
        source_ref=new_id("src"),
        raw_content=text,
        content_type="text",
        content_hash=hashlib.sha256(text.encode()).hexdigest(),
    )
    storage.store_raw_object(raw)
    knowledge = KnowledgeObject(
        id=new_id("ko"),
        user_id="alice",
        raw_object_id=raw.id,
        content=text,
        content_type="text",
        title=title,
        metadata_json={"document_date": document_date} if document_date else {},
    )
    storage.store_knowledge_object(knowledge)
    return knowledge.id


def test_different_days_of_a_course_are_not_duplicates(storage):
    left = _store(storage, text="План занятия. Тема 4.", title="12 день.docx")
    right = _store(storage, text="План занятия. Тема 5.", title="13 день.docx")

    reason = series_neighbour_reason(storage, "alice", left, right)

    assert "разные числа" in reason


def test_different_document_dates_are_not_duplicates(storage):
    left = _store(storage, text="Журнал приёма.", title="ЖП.pdf", document_date="2023-08-25")
    right = _store(storage, text="Журнал приёма. Смена.", title="ЖП.pdf", document_date="2023-08-24")

    reason = series_neighbour_reason(storage, "alice", left, right)

    assert "разные собственные даты" in reason


def test_a_marked_copy_is_still_a_copy(storage):
    """«_ копия» и «(1)» — это пометка копии, а не номер в серии."""

    left = _store(storage, text="Ориентировка. Варенцов.", title="ВАРЕНЦОВ Анатолий.docx")
    right = _store(storage, text="Ориентировка. Варенцов Анатолий.", title="ВАРЕНЦОВ Анатолий _ копия.docx")

    assert series_neighbour_reason(storage, "alice", left, right) == ""


def test_identical_text_wins_over_the_names(storage):
    """Тот же файл под разными именами — копия, как бы он ни назывался.

    Именно этот порядок спасает 18 верных решений на живом архиве: там «12 день»
    и «13 день» имели текст знак в знак.
    """

    left = _store(storage, text="Один и тот же текст.", title="12 день.docx")
    right = _store(storage, text="Один  и тот же   текст.", title="13 день.docx")

    assert series_neighbour_reason(storage, "alice", left, right) == ""


def test_a_missing_side_raises_no_objection(storage):
    """Нет документа — нет и суждения о нём: молчать, а не отсеивать вслепую."""

    left = _store(storage, text="Текст.", title="A.docx")

    assert series_neighbour_reason(storage, "alice", left, "ko_missing") == ""
