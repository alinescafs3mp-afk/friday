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

from friday.server import create_app
from friday.storage.models import KnowledgeObject, RawObject, new_id

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
    import copy
    import json
    import re
    from datetime import UTC, datetime, timedelta

    from friday.permissions import LEGACY_OWNER_USER_ID
    from friday.storage.models import Entity

    client, owner, user_id, ko_id, storage = instance
    foreign = "entity-confirm-foreign-positive-088"
    assert foreign != user_id
    storage.ensure_user(foreign, preset_key="user")
    own_sentinel = Entity(id=new_id("ent"), user_id=user_id, name="OWN_CONFIRM_SENTINEL")
    foreign_entity = Entity(
        id=new_id("ent"),
        user_id=foreign,
        name="POLARIS-02",
        entity_type="concept",
        metadata_json={"sentinel": "FOREIGN_CONFIRM_PRIVATE_CANARY"},
    )
    storage.create_entity(own_sentinel)
    storage.create_entity(foreign_entity)
    foreign_text = "FOREIGN_CONFIRM_DOCUMENT_CANARY"
    foreign_raw = RawObject(
        id=new_id("raw"),
        user_id=foreign,
        source="upload",
        source_ref=new_id("src"),
        raw_content=foreign_text,
        content_type="text",
        content_hash=hashlib.sha256(foreign_text.encode()).hexdigest(),
    )
    storage.store_raw_object(foreign_raw)
    foreign_knowledge = KnowledgeObject(
        id=new_id("ko"),
        user_id=foreign,
        raw_object_id=foreign_raw.id,
        content=foreign_text,
        content_type="text",
        title="Foreign sentinel",
    )
    storage.store_knowledge_object(foreign_knowledge)
    storage.link_knowledge_entity(
        foreign,
        foreign_knowledge.id,
        foreign_entity.id,
        evidence={"sentinel": "FOREIGN_CONFIRM_LINK_CANARY"},
        reviewed_by="foreign-reviewer",
    )
    # Native generated identifiers retain provenance through the writes above.
    ko_id = str(ko_id)
    foreign_entity_id = str(foreign_entity.id)
    tables = (
        "entities",
        "entity_versions",
        "knowledge_objects",
        "raw_objects",
        "knowledge_entity_links",
        "relation_candidates",
        "relations",
    )

    def same(actual, expected):
        assert type(actual) is type(expected)
        if isinstance(expected, dict):
            assert actual.keys() == expected.keys()
            for key in expected:
                same(actual[key], expected[key])
        elif isinstance(expected, list):
            assert len(actual) == len(expected)
            for actual_item, expected_item in zip(actual, expected, strict=True):
                same(actual_item, expected_item)
        else:
            assert actual == expected

    def generated(value, prefix):
        assert type(value) is str and re.fullmatch(prefix + r"_[0-9a-f]{16}", value)
        return value

    def moment(value, start, finish):
        assert type(value) is str
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        assert parsed.tzinfo is not None and parsed.utcoffset() == timedelta(0)
        assert start.replace(microsecond=0) <= parsed <= finish
        return value

    def rows():
        return {
            table: [
                dict(row)
                for row in storage.execute(
                    f"SELECT * FROM {table} WHERE user_id IN (?, ?) ORDER BY rowid", (user_id, foreign)
                ).fetchall()
            ]
            for table in tables
        }

    def audits():
        return [dict(row) for row in storage.execute("SELECT * FROM audit_log ORDER BY rowid").fetchall()]

    def assert_audit(previous, response, entity_id, created, start, finish):
        actual = audits()
        assert len(actual) == len(previous) + 1
        same(actual[:-1], previous)
        row = actual[-1]
        audit_id = generated(row["id"], "audit")
        assert audit_id not in {item["id"] for item in previous}
        request_id = response.headers["x-request-id"]
        assert re.fullmatch(r"[0-9a-f]{24}", request_id)
        same(
            row,
            {
                "id": audit_id,
                "user_id": LEGACY_OWNER_USER_ID,
                "action": "admin.entity_suggestion.accept",
                "target_type": "entity",
                "target_id": entity_id,
                "before_json": None,
                "after_json": json.dumps(
                    {
                        "name_chars": 10,
                        "entity_type": "concept",
                        "knowledge_object_id": ko_id,
                        "entity_created": created,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                "ip_address": "",
                "request_id": request_id,
                "created_at": moment(row["created_at"], start, finish),
            },
        )

    selected_before, audit_before = rows(), audits()
    assert [
        (row["id"], row["user_id"]) for row in selected_before["entities"] if row["name"] == "POLARIS-02"
    ] == [(foreign_entity_id, foreign)]
    own_knowledge = next(row for row in selected_before["knowledge_objects"] if row["id"] == ko_id)
    assert own_knowledge["user_id"] == user_id and own_knowledge["entity_id"] is None
    assert not [row for row in selected_before["knowledge_entity_links"] if row["user_id"] == user_id]
    before = storage.count_entities(user_id)

    start = datetime.now(UTC)
    response = client.post(
        f"/api/admin/knowledge/{ko_id}/entities",
        json={"user_id": user_id, "name": "POLARIS-02", "entity_type": "concept"},
        headers=owner,
    )
    finish = datetime.now(UTC)
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["entity_created"] is True
    assert storage.count_entities(user_id) == before + 1

    links = storage.list_knowledge_entity_links(user_id, knowledge_object_id=ko_id, limit=50)
    assert [link["status"] for link in links] == ["accepted"], (
        "подтверждённая человеком связь обязана быть утверждённой, а не предложенной"
    )

    actual = rows()
    old_entity_ids = {row["id"] for row in selected_before["entities"]}
    added_entities = [row for row in actual["entities"] if row["id"] not in old_entity_ids]
    assert len(added_entities) == 1
    new_entity = added_entities[0]
    entity_id = generated(new_entity["id"], "ent")
    expected_entity = {
        "id": entity_id,
        "user_id": user_id,
        "name": "POLARIS-02",
        "normalized_name": "polaris-02",
        "entity_type": "concept",
        "aliases_json": "[]",
        "description": "",
        "metadata_json": json.dumps(
            {"accepted_by": LEGACY_OWNER_USER_ID, "origin": "human_review"},
            ensure_ascii=False,
            sort_keys=True,
        ),
        "canonical": 1,
        "merged_into_id": None,
        "version": 1,
        "created_at": moment(new_entity["created_at"], start, finish),
        "updated_at": moment(new_entity["updated_at"], start, finish),
        "deleted_at": None,
    }
    old_version_ids = {row["id"] for row in selected_before["entity_versions"]}
    added_versions = [row for row in actual["entity_versions"] if row["id"] not in old_version_ids]
    assert len(added_versions) == 1
    version = added_versions[0]
    expected_version = {
        "id": generated(version["id"], "entv"),
        "user_id": user_id,
        "entity_id": entity_id,
        "version": 1,
        "snapshot_json": json.dumps(expected_entity, ensure_ascii=False, sort_keys=True),
        "created_at": moment(version["created_at"], start, finish),
    }
    old_link_ids = {row["id"] for row in selected_before["knowledge_entity_links"]}
    added_links = [row for row in actual["knowledge_entity_links"] if row["id"] not in old_link_ids]
    assert len(added_links) == 1
    link = added_links[0]
    link_id = generated(link["id"], "kel")
    linked_at = moment(link["created_at"], start, finish)
    expected_link = {
        "id": link_id,
        "user_id": user_id,
        "knowledge_object_id": ko_id,
        "entity_id": entity_id,
        "status": "accepted",
        "confidence": 1.0,
        "evidence_json": json.dumps(
            {"accepted_by": LEGACY_OWNER_USER_ID, "method": "human_review"},
            ensure_ascii=False,
            sort_keys=True,
        ),
        "created_at": linked_at,
        "reviewed_at": linked_at,
        "reviewed_by": LEGACY_OWNER_USER_ID,
    }
    expected = copy.deepcopy(selected_before)
    expected["entities"].append(expected_entity)
    expected["entity_versions"].append(expected_version)
    expected["knowledge_entity_links"].append(expected_link)
    expected_knowledge = next(row for row in expected["knowledge_objects"] if row["id"] == ko_id)
    expected_knowledge.update(entity_id=entity_id, updated_at=linked_at)
    same(actual, expected)
    public_link = {
        "id": link_id,
        "knowledge_object_id": ko_id,
        "entity_id": entity_id,
        "status": "accepted",
        "confidence": 1.0,
        "created_at": linked_at,
        "reviewed_at": linked_at,
        "entity_name": "POLARIS-02",
        "entity_type": "concept",
        "knowledge_title": "Записка",
        "knowledge_lifecycle": "active",
        "evidence": {"present": True, "bytes": 81},
    }
    same(
        payload,
        {
            "entity": expected_entity,
            "entity_created": True,
            "link": public_link,
            "relation_candidates": [],
        },
    )
    assert_audit(audit_before, response, entity_id, True, start, finish)

    # Canonical same-document reuse keeps the entity/version and legacy primary pointer.
    replay_audits = audits()
    start = datetime.now(UTC)
    replay = client.post(
        f"/api/admin/knowledge/{ko_id}/entities",
        json={"user_id": user_id, "name": "POLARIS-02", "entity_type": "concept"},
        headers=owner,
    )
    finish = datetime.now(UTC)
    assert replay.status_code == 200, replay.text
    actual = rows()
    replay_link = next(row for row in actual["knowledge_entity_links"] if row["id"] == link_id)
    reviewed_at = moment(replay_link["reviewed_at"], start, finish)
    expected_link["reviewed_at"] = reviewed_at
    same(actual, expected)
    public_link["reviewed_at"] = reviewed_at
    reused_entity = {key: value for key, value in expected_entity.items() if key != "normalized_name"}
    reused_entity["metadata_json"] = "{}"
    same(
        replay.json(),
        {
            "entity": reused_entity,
            "entity_created": False,
            "link": public_link,
            "relation_candidates": [],
        },
    )
    assert_audit(replay_audits, replay, entity_id, False, start, finish)


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
    import copy

    client, owner, user_id, ko_id, storage = instance
    foreign = "entity-confirm-foreign-088"
    storage.ensure_user(foreign, preset_key="user")

    def rows():
        tables = (
            "entities",
            "entity_versions",
            "knowledge_objects",
            "raw_objects",
            "knowledge_entity_links",
            "relation_candidates",
            "relations",
        )
        selected = {
            table: [
                dict(row)
                for row in storage.execute(
                    f"SELECT * FROM {table} WHERE user_id IN (?, ?) ORDER BY rowid",
                    (user_id, foreign),
                ).fetchall()
            ]
            for table in tables
        }
        selected["audit_log"] = [
            dict(row) for row in storage.execute("SELECT * FROM audit_log ORDER BY rowid").fetchall()
        ]
        return selected

    cases = (
        (str(ko_id), user_id, "мысль", 400, "Неизвестный entity_type: мысль"),
        ("ko_0000000000000000", user_id, "concept", 404, "Объект знания не найден"),
        (str(ko_id), foreign, "concept", 404, "Объект знания не найден"),
    )
    for knowledge_id, target, kind, status, detail in cases:
        before = copy.deepcopy(rows())
        response = client.post(
            f"/api/admin/knowledge/{knowledge_id}/entities",
            json={"user_id": target, "name": "POLARIS-02", "entity_type": kind},
            headers=owner,
        )
        assert response.status_code == status, response.text
        assert response.json() == {"detail": detail}
        assert rows() == before


def test_confirming_is_written_to_the_audit_log(instance):
    client, owner, user_id, ko_id, storage = instance
    client.post(
        f"/api/admin/knowledge/{ko_id}/entities",
        json={"user_id": user_id, "name": "POLARIS-02", "entity_type": "concept"},
        headers=owner,
    )
    actions = [row["action"] for row in storage.list_audit_log(None, limit=50)]
    assert "admin.entity_suggestion.accept" in actions


def test_candidates_come_back_strongest_first(instance):
    """На живом документе из 23 кандидатов годными были единицы.

    `location_preposition` (0.77) принимает «в Наставлении» и «в Курсе» за места, и
    в списке, где годное перемешано с шумом, человек бросает разбор. Порядок по
    уверенности ставит объявленные методы (0.89–0.93) наверх и не стоит ничего.
    """
    client, owner, user_id, ko_id, _ = instance
    items = client.get(
        f"/api/admin/knowledge/{ko_id}/entity-suggestions?user_id={user_id}", headers=owner
    ).json()["items"]

    scores = [float(item.get("confidence") or 0) for item in items]
    assert scores == sorted(scores, reverse=True), f"порядок не по уверенности: {scores}"
