"""Кандидат в сущность не имел пути в граф, и вся цепочка стояла из-за этого.

Сущность создаётся автоматически только при уверенности ≥ 0.88. На настоящем
корпусе владельца (1605 документов) два метода дают почти всё:

    capitalized_person_name   5797 кандидатов, уверенность 0.76
    identifier_syntax         3907 кандидатов, уверенность 0.75
    все объявленные методы     157

Порог поднят НАМЕРЕННО: в v0.99.0 замерили, что из 28 автопринятых связей 26 давал
`identifier_syntax`, и вещами была примерно четверть. Опускать его — вернуть мусор
в граф.

Значит кандидату нужен другой путь — через человека. Его не было: кандидаты нигде
не сохранялись (в метаданных лежало только их число), маршрута подтверждения не
существовало, кнопки тоже. Цепочка «извлекли → человек подтвердил → нашлась связь»
была разомкнута на первом звене, и проверено на живом: документ, где по замеру
должно найтись шесть связей, дал НОЛЬ связей с сущностями.
"""

from __future__ import annotations

import hashlib

import pytest
from fastapi.testclient import TestClient

from jericho.server import create_app
from jericho.storage.models import KnowledgeObject, RawObject, new_id

# Текст без объявляющих слов («проект X», «сервис Y») — то есть ровно такой, какой
# приезжает из рабочего архива и из которого автомат сущностей не создаёт.
# Ровно тот случай, ради которого поверхность и делалась: `ATLAS-01` набирает 0.89
# и создаётся автоматически, `POLARIS-02` — 0.75 и не создаётся никогда. Между ними
# стоит связка «использует», то есть связь есть и найтись не может.
TEXT = "Сервис ATLAS-01 использует базу POLARIS-02 для хранения смет."


@pytest.fixture
def instance(settings):
    app = create_app(settings)
    with TestClient(app) as client:
        storage = app.state.storage
        owner = {"Authorization": f"Bearer {settings.api_token}"}
        user_id = client.get("/api/admin/users", headers=owner).json()["items"][0]["id"]
        raw = RawObject(
            id=new_id("raw"),
            user_id=user_id,
            source="upload",
            source_ref=new_id("src"),
            raw_content=TEXT,
            content_type="text",
            content_hash=hashlib.sha256(TEXT.encode()).hexdigest(),
        )
        storage.store_raw_object(raw)
        ko = KnowledgeObject(
            id=new_id("ko"),
            user_id=user_id,
            raw_object_id=raw.id,
            content=TEXT,
            content_type="text",
            title="Записка",
        )
        storage.store_knowledge_object(ko)
        yield client, owner, user_id, ko.id, storage


def test_the_document_offers_its_candidates(instance):
    client, owner, user_id, ko_id, _ = instance
    response = client.get(f"/api/admin/knowledge/{ko_id}/entity-suggestions?user_id={user_id}", headers=owner)
    assert response.status_code == 200, response.text
    names = {item["name"] for item in response.json()["items"]}
    assert names, "документ не предложил ни одного кандидата — стенд собран неверно"


def test_confirming_a_candidate_creates_the_node_and_an_accepted_link(instance):
    client, owner, user_id, ko_id, storage = instance
    before = storage.count_entities(user_id)

    response = client.post(
        f"/api/admin/knowledge/{ko_id}/entities",
        json={"user_id": user_id, "name": "POLARIS-02", "entity_type": "concept"},
        headers=owner,
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["entity_created"] is True
    assert storage.count_entities(user_id) == before + 1

    links = storage.list_knowledge_entity_links(user_id, knowledge_object_id=ko_id, limit=50)
    assert [link["status"] for link in links] == ["accepted"], (
        "подтверждённая человеком связь обязана быть утверждённой, а не предложенной"
    )


def test_confirming_the_second_entity_finds_the_relation(instance):
    """То, ради чего вся цепочка: два подтверждения — и связь между ними предложена."""
    client, owner, user_id, ko_id, storage = instance
    for name in ("ATLAS-01", "POLARIS-02"):
        response = client.post(
            f"/api/admin/knowledge/{ko_id}/entities",
            json={"user_id": user_id, "name": name, "entity_type": "concept"},
            headers=owner,
        )
        assert response.status_code == 200, response.text

    assert storage.count_relation_candidates(user_id) > 0, (
        "две подтверждённые сущности и фраза «использует» между ними не дали связи"
    )
    assert response.json()["relation_candidates"], "ответ не сообщил о найденной связи"


def test_the_same_name_reuses_its_node_across_documents(instance):
    """Иначе подтверждение одного имени в двух документах плодит двойников."""
    client, owner, user_id, ko_id, storage = instance
    first = client.post(
        f"/api/admin/knowledge/{ko_id}/entities",
        json={"user_id": user_id, "name": "POLARIS-02", "entity_type": "concept"},
        headers=owner,
    ).json()
    second = client.post(
        f"/api/admin/knowledge/{ko_id}/entities",
        json={"user_id": user_id, "name": "POLARIS-02", "entity_type": "concept"},
        headers=owner,
    ).json()

    assert second["entity_created"] is False
    assert second["entity"]["id"] == first["entity"]["id"]


def test_a_decided_candidate_is_not_offered_again(instance):
    """Просить одно и то же решение дважды — способ обесценить очередь разбора."""
    client, owner, user_id, ko_id, _ = instance
    client.post(
        f"/api/admin/knowledge/{ko_id}/entities",
        json={"user_id": user_id, "name": "POLARIS-02", "entity_type": "concept"},
        headers=owner,
    )
    remaining = client.get(
        f"/api/admin/knowledge/{ko_id}/entity-suggestions?user_id={user_id}", headers=owner
    ).json()
    assert "POLARIS-02" not in {item["name"] for item in remaining["items"]}
    assert remaining["decided"] >= 1


def test_an_unknown_entity_type_is_refused_by_name(instance):
    client, owner, user_id, ko_id, _ = instance
    response = client.post(
        f"/api/admin/knowledge/{ko_id}/entities",
        json={"user_id": user_id, "name": "POLARIS-02", "entity_type": "мысль"},
        headers=owner,
    )
    assert response.status_code == 400
    assert "Unknown entity_type" in response.text


def test_confirming_is_written_to_the_audit_log(instance):
    client, owner, user_id, ko_id, storage = instance
    client.post(
        f"/api/admin/knowledge/{ko_id}/entities",
        json={"user_id": user_id, "name": "POLARIS-02", "entity_type": "concept"},
        headers=owner,
    )
    actions = [row["action"] for row in storage.list_audit_log(None, limit=50)]
    assert "admin.entity_suggestion.accept" in actions
