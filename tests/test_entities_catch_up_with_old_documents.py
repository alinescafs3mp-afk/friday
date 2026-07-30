"""Сущность, родившаяся поздно, не возвращалась к старым документам НИКОГДА.

Связи ставятся только в момент разбора документа. Значит сущность, появившаяся на
девятисотом документе, к первым восьмистам не приходит: обратного прохода не было ни
в API, ни в CLI.

Замерено на архиве владельца: **1173 пары (документ, сущность), где имя стоит в тексте
дословно, а связи нет**; затронуто 645 документов. Документов, где встречается хотя бы
одна известная сущность, — 710, а связи есть у 92. Это же и есть главная причина, по
которой граф не растёт.

Человеческого решения проход не требует: `existing_entity_exact_mention` с уверенностью
0.97 входит в `DECLARED_ENTITY_METHODS`, то есть при разборе принимается автоматически.
"""

from __future__ import annotations

import hashlib

from jericho.storage.models import Entity, EntityType, KnowledgeObject, RawObject, new_id


def _document(storage, user_id: str, index: int, text: str) -> str:
    raw = RawObject(
        id=new_id("raw"),
        user_id=user_id,
        source="t",
        source_ref=new_id("s"),
        raw_content=text,
        content_type="text",
        content_hash=hashlib.sha256(f"{user_id}-{index}".encode()).hexdigest(),
    )
    storage.store_raw_object(raw)
    knowledge = KnowledgeObject(
        id=new_id("ko"),
        user_id=user_id,
        raw_object_id=raw.id,
        content=text,
        content_type="text",
        title=f"Документ {index}",
    )
    storage.store_knowledge_object(knowledge)
    return knowledge.id


def _entity(storage, user_id: str, name: str, *, aliases: list[str] | None = None) -> str:
    entity = Entity(
        id=new_id("ent"),
        user_id=user_id,
        name=name,
        entity_type=EntityType.ORGANIZATION,
        aliases_json=aliases or [],
    )
    storage.create_entity(entity)
    return entity.id


def test_an_entity_born_late_reaches_the_documents_that_precede_it(storage):
    storage.ensure_user("alice")
    old = _document(storage, "alice", 1, "Поставку выполнил Комбинат в срок.")
    unrelated = _document(storage, "alice", 2, "Ведомость расчёта за квартал.")
    entity_id = _entity(storage, "alice", "Комбинат")

    report = storage.backfill_entity_mentions("alice")

    assert report["linked"] == 1, f"обратный проход не связал старый документ: {report}"
    linked = {
        str(link["knowledge_object_id"])
        for link in storage.list_knowledge_entity_links("alice", entity_id=entity_id)
    }
    assert linked == {old}
    assert unrelated not in linked


def test_a_rejected_link_is_never_resurrected(storage):
    """Главное ограничение прохода.

    `link_knowledge_entity` перезаписывает статус, поэтому без проверки обратный ход
    воскресил бы связи, которые человек отклонил. Ровно тот класс ошибок, который в
    этом проекте закрывали трижды.
    """
    storage.ensure_user("alice")
    document = _document(storage, "alice", 1, "Речь про Комбинат и его планы.")
    entity_id = _entity(storage, "alice", "Комбинат")
    storage.link_knowledge_entity("alice", document, entity_id, status="rejected", reviewed_by="alice")

    report = storage.backfill_entity_mentions("alice")

    assert report["linked"] == 0
    links = storage.list_knowledge_entity_links("alice", entity_id=entity_id, status=None)
    assert [str(link["status"]) for link in links] == ["rejected"], "отклонённая связь воскрешена"


def test_an_existing_accepted_link_is_not_duplicated(storage):
    storage.ensure_user("alice")
    document = _document(storage, "alice", 1, "Комбинат отчитался.")
    entity_id = _entity(storage, "alice", "Комбинат")
    storage.link_knowledge_entity("alice", document, entity_id, status="accepted")

    report = storage.backfill_entity_mentions("alice")
    assert report["linked"] == 0


def test_aliases_count_as_mentions(storage):
    """Псевдоним — то же имя; при разборе он тоже срабатывает."""
    storage.ensure_user("alice")
    document = _document(storage, "alice", 1, "Работы вёл КМК по договору.")
    entity_id = _entity(storage, "alice", "Комбинат", aliases=["КМК"])

    assert storage.backfill_entity_mentions("alice")["linked"] == 1
    assert storage.list_knowledge_entity_links("alice", entity_id=entity_id)


def test_a_substring_is_not_a_mention(storage):
    """Границы слов те же, что при разборе: иначе задним числом появятся связи,
    которых обычный путь не создал бы."""
    storage.ensure_user("alice")
    _document(storage, "alice", 1, "Документ про суперкомбинатное оборудование.")
    _entity(storage, "alice", "Комбинат")

    assert storage.backfill_entity_mentions("alice")["linked"] == 0


def test_the_sweep_resumes_and_reports_completion(storage):
    """Обход возобновляемый: на большом архиве полный проход дорог."""
    storage.ensure_user("alice")
    for index in range(6):
        _document(storage, "alice", index, "Комбинат упомянут здесь.")
    _entity(storage, "alice", "Комбинат")

    first = storage.backfill_entity_mentions("alice", max_documents=2)
    assert first["scanned"] == 2 and first["linked"] == 2 and first["complete"] is False

    second = storage.backfill_entity_mentions("alice", max_documents=2)
    assert second["linked"] == 2, "обход не продолжился с того же места"

    storage.backfill_entity_mentions("alice", max_documents=10)
    done = storage.backfill_entity_mentions("alice", max_documents=10)
    assert done["complete"] is True, "обход не сообщил о завершении круга"


def test_an_archive_without_entities_does_nothing(storage):
    storage.ensure_user("alice")
    _document(storage, "alice", 1, "Текст без сущностей.")
    report = storage.backfill_entity_mentions("alice")
    assert report == {"linked": 0, "scanned": 0, "complete": True, "entities": 0}
