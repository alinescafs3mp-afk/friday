"""Проход правилом ФИО по уже загруженному архиву.

Правило `explicit_person_patronymic` появилось после того, как корпус владельца был
загружен, и на существующие документы не действовало: в графе было 110 сущностей и ни
одного человека, при том что почти все вопросы владельца — про людей. Проход закрывает
именно это.

Замер перед применением (900 документов живого корпуса): 6094 упоминания, 3069 разных
ФИО; на всех 1532 объектах — 20644 упоминания и 4349 узлов. Перестановочных двойников
(«Фамилия Имя Отчество» против «Имя Отчество Фамилия») оказалось 4 имени из 4349, то
есть 0.1% при объявленном заранее пороге 10%.

Главное, что здесь проверяется, — НЕ то, что проход находит имена (это работа регулярки,
проверенной отдельно), а то, что он **не спорит с человеком**. `link_knowledge_entity`
перезаписывает статус по `ON CONFLICT`, поэтому проход, идущий по всему корпусу, мог бы
молча вернуть отклонённой человеком связи статус `accepted` — и владелец увидел бы, что
его решение отменили без объяснений.
"""

from __future__ import annotations

import argparse
import hashlib

from friday.storage.models import EntityType, KnowledgeObject, RawObject, new_id

# Отчество делает эти строки ОБЪЯВЛЕНИЕМ имени, а не парой слов с большой буквы.
BODY = (
    "Приказом от 3 марта назначен Петров Иван Иванович, а обязанности принял "
    "Сидоров Пётр Никифорович. Ознакомлен Кузнецова Анна Сергеевна."
)


def _store(storage, user_id: str, text: str) -> str:
    raw = RawObject(
        id=new_id("raw"),
        user_id=user_id,
        source="test",
        source_ref=new_id("src"),
        raw_content=text,
        content_type="text",
        content_hash=hashlib.sha256(text.encode()).hexdigest(),
    )
    storage.store_raw_object(raw)
    knowledge = KnowledgeObject(
        id=new_id("ko"),
        user_id=user_id,
        raw_object_id=raw.id,
        content=text,
        content_type="text",
        title="Приказ",
    )
    storage.store_knowledge_object(knowledge)
    return knowledge.id


def _run(**overrides) -> int:
    from friday.cli import _backfill_entities

    # Метод называется явно: команда обобщена под любое ОБЪЯВЛЯЮЩЕЕ правило, а не
    # только под ФИО — см. `backfill-entities` и правило войсковой части.
    args = argparse.Namespace(method="explicit_person_patronymic", user=None, batch=50, limit=0, apply=True)
    for key, value in overrides.items():
        setattr(args, key, value)
    return _backfill_entities(args)


def test_the_pass_creates_people_for_documents_ingested_before_the_rule(settings, storage):
    """Документ, лежавший в архиве до появления правила, получает узлы-людей."""
    storage.ensure_user("alice")
    ko_id = _store(storage, "alice", BODY)

    assert _run() == 0

    people = [
        row
        for row in storage.list_entities("alice", limit=100)
        if row["entity_type"] == EntityType.PERSON.value
    ]
    assert len(people) == 3, f"ожидались три человека, получено {len(people)}"
    linked = {
        str(row["entity_id"])
        for row in storage.list_knowledge_entity_links("alice", knowledge_object_id=ko_id)
    }
    assert linked == {str(row["id"]) for row in people}


def test_the_pass_writes_nothing_without_apply(settings, storage):
    """Режим показа обязан быть по-настоящему безмолвным: это первый прогон на живой
    базе владельца, и его смотрят глазами прежде, чем разрешить запись."""
    storage.ensure_user("alice")
    _store(storage, "alice", BODY)

    assert _run(apply=False) == 0

    assert storage.list_entities("alice", limit=100) == []


def test_the_pass_does_not_overturn_a_decision_the_owner_already_made(settings, storage):
    """Отклонённая человеком связь остаётся отклонённой.

    Мутация, которую этот тест обязан ловить: убрать обращение к
    `decided_entity_links` в `_backfill_person_entities`. Тогда `ON CONFLICT DO UPDATE`
    внутри `link_knowledge_entity` перепишет `rejected` на `accepted`, и решение
    владельца исчезнет без следа — при том, что все счётчики прохода останутся
    правдоподобными.
    """
    storage.ensure_user("alice")
    ko_id = _store(storage, "alice", BODY)

    # Первый проход заводит людей и связи.
    assert _run() == 0
    links = storage.list_knowledge_entity_links("alice", knowledge_object_id=ko_id)
    assert links, "не на чем проверять: связей не появилось"
    rejected_entity = str(links[0]["entity_id"])

    # Владелец говорит: этот человек к документу отношения не имеет.
    storage.link_knowledge_entity(
        "alice",
        ko_id,
        rejected_entity,
        status="rejected",
        confidence=0.9,
        reviewed_by="alice",
    )

    # Второй проход не имеет права это отменить.
    assert _run() == 0

    # `status=None`, а не значение по умолчанию: список отдаёт только `accepted`, и
    # проверка «его нет среди принятых» прошла бы даже на удалённой строке. Спрашиваем
    # именно про статус этой пары.
    after = {
        str(row["entity_id"]): str(row["status"])
        for row in storage.list_knowledge_entity_links("alice", knowledge_object_id=ko_id, status=None)
    }
    assert after[rejected_entity] == "rejected", "проход отменил решение владельца"
