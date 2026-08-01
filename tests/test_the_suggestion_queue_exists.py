"""Экраном подтверждения сущностей не воспользовались НИ РАЗУ — и не потому, что отказались.

Проверено по живой базе: ни одна из 109 сущностей и ни одна из 226 связей не пришла от
человека. У всех сущностей в метаданных ключи автосоздания при импорте; ключа
`origin: human_review`, который ставит обработчик подтверждения, нет ни у одной. В
аудите нет ни одной записи `admin.entity_suggestion.accept`.

Причина не в отказе, а в том, что предложить было НЕГДЕ: кандидаты считаются по
запросу и нигде не хранятся, поэтому их нельзя было ни посчитать, ни показать. На
обзоре шесть плиток, числа кандидатов среди них нет; в разделе «Граф» четыре очереди
на проверку, и этой среди них тоже нет. Единственный вход — открыть конкретный документ
и нажать «Инспекция», то есть надо было заранее знать, куда идти.

При этом материал есть: `entity_suggestion_count` записан у 1532 объектов из 1537,
всего 10 100 предложений, медиана 7 на документ.
"""

from __future__ import annotations

import hashlib

from friday.storage.models import Entity, EntityType, KnowledgeObject, RawObject, new_id


def _document(storage, user_id: str, index: int, suggestions: int) -> str:
    text = f"Документ {index}. " * 10
    raw = RawObject(
        id=new_id("raw"),
        user_id=user_id,
        source="t",
        source_ref=new_id("s"),
        raw_content=text,
        content_type="text",
        content_hash=hashlib.sha256(f"{index}".encode()).hexdigest(),
    )
    storage.store_raw_object(raw)
    knowledge = KnowledgeObject(
        id=new_id("ko"),
        user_id=user_id,
        raw_object_id=raw.id,
        content=text,
        content_type="text",
        title=f"Документ {index}",
        metadata_json={"entity_suggestion_count": suggestions},
    )
    storage.store_knowledge_object(knowledge)
    return knowledge.id


def test_documents_with_pending_suggestions_are_listed_by_weight(storage):
    storage.ensure_user("alice")
    small = _document(storage, "alice", 1, 2)
    big = _document(storage, "alice", 2, 9)
    _document(storage, "alice", 3, 0)

    items, total = storage.list_documents_with_entity_suggestions("alice")

    assert total == 2
    assert [item["id"] for item in items] == [big, small], "порядок не по числу неразобранного"
    assert items[0]["pending"] == 9


def test_confirmed_links_reduce_what_is_left(storage):
    """Иначе очередь показывала бы одно и то же после каждого разбора."""
    storage.ensure_user("alice")
    ko_id = _document(storage, "alice", 1, 3)
    entity = Entity(id=new_id("ent"), user_id="alice", name="Комбинат", entity_type=EntityType.ORGANIZATION)
    storage.create_entity(entity)
    storage.link_knowledge_entity("alice", ko_id, entity.id, status="accepted")

    items, _ = storage.list_documents_with_entity_suggestions("alice")
    assert items[0]["pending"] == 2


def test_a_fully_resolved_document_leaves_the_queue(storage):
    storage.ensure_user("alice")
    ko_id = _document(storage, "alice", 1, 1)
    entity = Entity(id=new_id("ent"), user_id="alice", name="Комбинат", entity_type=EntityType.ORGANIZATION)
    storage.create_entity(entity)
    storage.link_knowledge_entity("alice", ko_id, entity.id, status="accepted")

    items, total = storage.list_documents_with_entity_suggestions("alice")
    assert total == 0 and items == []


def test_a_rejected_link_also_counts_as_decided(storage):
    """Человек посмотрел и сказал «нет» — это разбор, а не пропуск."""
    storage.ensure_user("alice")
    ko_id = _document(storage, "alice", 1, 1)
    entity = Entity(id=new_id("ent"), user_id="alice", name="Комбинат", entity_type=EntityType.ORGANIZATION)
    storage.create_entity(entity)
    storage.link_knowledge_entity("alice", ko_id, entity.id, status="rejected", reviewed_by="alice")

    _, total = storage.list_documents_with_entity_suggestions("alice")
    assert total == 0


def test_a_document_without_the_stored_count_is_not_offered(storage):
    """Старые записи без метки — не повод показывать пустую работу."""
    storage.ensure_user("alice")
    text = "Документ без метки. " * 10
    raw = RawObject(
        id=new_id("raw"),
        user_id="alice",
        source="t",
        source_ref=new_id("s"),
        raw_content=text,
        content_type="text",
        content_hash=hashlib.sha256(b"x").hexdigest(),
    )
    storage.store_raw_object(raw)
    storage.store_knowledge_object(
        KnowledgeObject(
            id=new_id("ko"),
            user_id="alice",
            raw_object_id=raw.id,
            content=text,
            content_type="text",
            title="Без метки",
        )
    )

    _, total = storage.list_documents_with_entity_suggestions("alice")
    assert total == 0


def test_the_route_says_the_number_is_an_estimate(settings, storage):
    """Предложение и связь — не одно и то же, и называть оценку точным остатком нельзя."""
    from fastapi.testclient import TestClient

    from friday.server import create_app

    storage.ensure_user("alice")
    _document(storage, "alice", 1, 5)

    with TestClient(create_app(settings)) as client:
        response = client.get(
            "/api/admin/entity-suggestions/queue?user_id=alice",
            headers={"Authorization": f"Bearer {settings.api_token}"},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["total"] == 1
        assert body["estimate"] is True


# --- группы: одно решение вместо N -------------------------------------------


def _document_with_text(storage, user_id: str, index: int, text: str) -> str:
    raw = RawObject(
        id=new_id("raw"),
        user_id=user_id,
        source="t",
        source_ref=new_id("s"),
        raw_content=text,
        content_type="text",
        content_hash=hashlib.sha256(f"g{index}".encode()).hexdigest(),
    )
    storage.store_raw_object(raw)
    knowledge = KnowledgeObject(
        id=new_id("ko"),
        user_id=user_id,
        raw_object_id=raw.id,
        content=text,
        content_type="text",
        title=f"Групповой документ {index}",
        metadata_json={"entity_suggestion_count": 3},
    )
    storage.store_knowledge_object(knowledge)
    return knowledge.id


def test_groups_collect_one_entity_across_documents(settings, storage):
    """42 кандидата на документ делают поштучный разбор нечитаемым из-за объёма;
    «Казань в 57 документах» — одно решение, а не 57."""
    from fastapi.testclient import TestClient

    from friday.server import create_app

    storage.ensure_user("alice")
    _document_with_text(storage, "alice", 1, "Сервис ATLAS-01 обслуживает узел связи по графику.")
    _document_with_text(storage, "alice", 2, "Регламентные работы сервиса ATLAS-01 завершены в срок.")

    with TestClient(create_app(settings)) as client:
        response = client.get(
            "/api/admin/entity-suggestions/groups?user_id=alice&min_docs=2",
            headers={"Authorization": f"Bearer {settings.api_token}"},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["estimate"] is True
        assert body["scanned_documents"] == 2
        group = next((g for g in body["groups"] if "ATLAS-01" in g["name"]), None)
        assert group is not None, f"группа не собралась: {[g['name'] for g in body['groups']]}"
        assert group["document_count"] == 2


def test_group_accept_is_one_decision_for_every_document(settings, storage):
    from fastapi.testclient import TestClient

    from friday.server import create_app

    storage.ensure_user("alice")
    first = _document_with_text(storage, "alice", 3, "Сервис ATLAS-02 обслуживает узел связи.")
    second = _document_with_text(storage, "alice", 4, "Отчёт по сервису ATLAS-02 подписан.")

    with TestClient(create_app(settings)) as client:
        headers = {"Authorization": f"Bearer {settings.api_token}"}
        decided = client.post(
            "/api/admin/entity-suggestions/groups/decide",
            json={
                "user_id": "alice",
                "name": "ATLAS-02",
                "entity_type": "concept",
                "decision": "accept",
                "knowledge_object_ids": [first, second],
            },
            headers=headers,
        )
        assert decided.status_code == 200, decided.text
        body = decided.json()
        assert body["decided"] == 2 and body["entity_created"] is True

        app_storage = client.app.state.storage
        entity = app_storage.find_entity_by_name("alice", "ATLAS-02")
        assert entity is not None
        links = app_storage.list_knowledge_entity_links("alice", entity_id=str(entity["id"]))
        assert len(links) == 2
        assert all(link["status"] == "accepted" for link in links)

        # Повтор той же группы ничего не перезаписывает: решённое решено.
        replay = client.post(
            "/api/admin/entity-suggestions/groups/decide",
            json={
                "user_id": "alice",
                "name": "ATLAS-02",
                "entity_type": "concept",
                "decision": "accept",
                "knowledge_object_ids": [first, second],
            },
            headers=headers,
        )
        assert replay.json()["decided"] == 0
        assert replay.json()["skipped_existing"] == 2


def test_group_reject_records_refusal_without_creating_a_node(settings, storage):
    """Отклонение группы без узла не должно СОЗДАВАТЬ узел ради записи отказа."""
    from fastapi.testclient import TestClient

    from friday.server import create_app

    storage.ensure_user("alice")
    first = _document_with_text(storage, "alice", 5, "В тексте встречается в Наставлении по связи.")

    with TestClient(create_app(settings)) as client:
        headers = {"Authorization": f"Bearer {settings.api_token}"}
        rejected = client.post(
            "/api/admin/entity-suggestions/groups/decide",
            json={
                "user_id": "alice",
                "name": "Наставлении",
                "entity_type": "location",
                "decision": "reject",
                "knowledge_object_ids": [first],
            },
            headers=headers,
        )
        assert rejected.status_code == 200, rejected.text
        assert rejected.json()["entity"] is None
        assert rejected.json()["decided"] == 0
        assert client.app.state.storage.find_entity_by_name("alice", "Наставлении") is None
