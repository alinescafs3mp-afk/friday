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
    import copy
    import json
    import re
    from datetime import UTC, datetime, timedelta

    from fastapi.testclient import TestClient

    from friday.permissions import LEGACY_OWNER_USER_ID
    from friday.server import create_app
    from friday.storage._knowledge import _bounded_public_knowledge_entity_links

    storage.ensure_user("alice")
    foreign = "entity-group-foreign-accept-088"
    storage.ensure_user(foreign, preset_key="user")
    first_native = _document_with_text(storage, "alice", 3, "Сервис ATLAS-02 обслуживает узел связи.")
    second_native = _document_with_text(storage, "alice", 4, "Отчёт по сервису ATLAS-02 подписан.")
    foreign_document_native = _document_with_text(storage, foreign, 104, "Чужой отчёт по сервису ATLAS-02.")
    own_sentinel = Entity(
        id=new_id("ent"),
        user_id="alice",
        name="OWN_GROUP_ACCEPT_SENTINEL",
        metadata_json={"sentinel": "OWN_GROUP_ACCEPT_PRIVATE_CANARY"},
    )
    foreign_entity = Entity(
        id=new_id("ent"),
        user_id=foreign,
        name="ATLAS-02",
        entity_type=EntityType.CONCEPT,
        metadata_json={"sentinel": "FOREIGN_GROUP_ACCEPT_PRIVATE_CANARY"},
    )
    storage.create_entity(own_sentinel)
    storage.create_entity(foreign_entity)
    storage.link_knowledge_entity(
        foreign,
        foreign_document_native,
        foreign_entity.id,
        status="accepted",
        evidence={"sentinel": "FOREIGN_GROUP_ACCEPT_LINK_CANARY"},
        reviewed_by="foreign-reviewer",
    )
    # Generated IDs retain native provenance through every fixture write above.
    first, second = str(first_native), str(second_native)
    foreign_document, foreign_entity_id = str(foreign_document_native), str(foreign_entity.id)
    assert type(first) is str and type(second) is str and type(foreign_document) is str
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

    with TestClient(create_app(settings)) as client:
        headers = {"Authorization": f"Bearer {settings.api_token}"}
        app_storage = client.app.state.storage

        def rows():
            return {
                table: [
                    dict(row)
                    for row in app_storage.execute(
                        f"SELECT * FROM {table} WHERE user_id IN (?, ?) ORDER BY rowid",
                        ("alice", foreign),
                    ).fetchall()
                ]
                for table in tables
            }

        def audits():
            return [
                dict(row) for row in app_storage.execute("SELECT * FROM audit_log ORDER BY rowid").fetchall()
            ]

        def assert_audit(previous, response, entity_id, after, start, finish):
            actual = audits()
            assert len(actual) == len(previous) + 1
            same(actual[:-1], previous)
            row = actual[-1]
            audit_id = generated(row["id"], "audit")
            assert audit_id not in {item["id"] for item in previous}
            request_id = response.headers["x-request-id"]
            assert type(request_id) is str and re.fullmatch(r"[0-9a-f]{24}", request_id)
            same(
                row,
                {
                    "id": audit_id,
                    "user_id": LEGACY_OWNER_USER_ID,
                    "action": "admin.entity_suggestion.bulk_accept",
                    "target_type": "entity",
                    "target_id": entity_id,
                    "before_json": None,
                    "after_json": json.dumps(after, ensure_ascii=False, sort_keys=True),
                    "ip_address": "",
                    "request_id": request_id,
                    "created_at": moment(row["created_at"], start, finish),
                },
            )

        selected_before, audit_before = rows(), audits()
        assert [
            (row["id"], row["user_id"]) for row in selected_before["entities"] if row["name"] == "ATLAS-02"
        ] == [(foreign_entity_id, foreign)]
        for knowledge_id in (first, second):
            own_document = next(
                row for row in selected_before["knowledge_objects"] if row["id"] == knowledge_id
            )
            assert own_document["user_id"] == "alice" and own_document["entity_id"] is None
        assert not [row for row in selected_before["knowledge_entity_links"] if row["user_id"] == "alice"]

        start = datetime.now(UTC)
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
        finish = datetime.now(UTC)
        assert decided.status_code == 200, decided.text
        body = decided.json()
        assert body["decided"] == 2 and body["entity_created"] is True

        entity = app_storage.find_entity_by_name("alice", "ATLAS-02")
        assert entity is not None
        links = app_storage.list_knowledge_entity_links("alice", entity_id=str(entity["id"]))
        assert len(links) == 2
        assert all(link["status"] == "accepted" for link in links)

        actual = rows()
        old_entity_ids = {row["id"] for row in selected_before["entities"]}
        added_entities = [row for row in actual["entities"] if row["id"] not in old_entity_ids]
        assert len(added_entities) == 1
        new_entity = added_entities[0]
        entity_id = generated(new_entity["id"], "ent")
        expected_entity = {
            "id": entity_id,
            "user_id": "alice",
            "name": "ATLAS-02",
            "normalized_name": "atlas-02",
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
        same(new_entity, expected_entity)
        old_version_ids = {row["id"] for row in selected_before["entity_versions"]}
        added_versions = [row for row in actual["entity_versions"] if row["id"] not in old_version_ids]
        assert len(added_versions) == 1
        version = added_versions[0]
        expected_version = {
            "id": generated(version["id"], "entv"),
            "user_id": "alice",
            "entity_id": entity_id,
            "version": 1,
            "snapshot_json": json.dumps(expected_entity, ensure_ascii=False, sort_keys=True),
            "created_at": moment(version["created_at"], start, finish),
        }
        same(version, expected_version)

        evidence_json = json.dumps(
            {"accepted_by": LEGACY_OWNER_USER_ID, "method": "human_review_bulk"},
            ensure_ascii=False,
            sort_keys=True,
        )
        assert len(evidence_json.encode("utf-8")) == 86
        old_link_ids = {row["id"] for row in selected_before["knowledge_entity_links"]}
        added_links = [row for row in actual["knowledge_entity_links"] if row["id"] not in old_link_ids]
        assert len(added_links) == 2
        links_by_document = {row["knowledge_object_id"]: row for row in added_links}
        assert links_by_document.keys() == {first, second}
        expected_links = []
        public_cards = {}
        for knowledge_id, title in ((first, "Групповой документ 3"), (second, "Групповой документ 4")):
            link = links_by_document[knowledge_id]
            link_id = generated(link["id"], "kel")
            linked_at = moment(link["created_at"], start, finish)
            expected_link = {
                "id": link_id,
                "user_id": "alice",
                "knowledge_object_id": knowledge_id,
                "entity_id": entity_id,
                "status": "accepted",
                "confidence": 1.0,
                "evidence_json": evidence_json,
                "created_at": linked_at,
                "reviewed_at": linked_at,
                "reviewed_by": LEGACY_OWNER_USER_ID,
            }
            same(link, expected_link)
            expected_links.append(expected_link)
            public_cards[knowledge_id] = {
                "id": link_id,
                "knowledge_object_id": knowledge_id,
                "entity_id": entity_id,
                "status": "accepted",
                "confidence": 1.0,
                "created_at": linked_at,
                "reviewed_at": linked_at,
                "entity_name": "ATLAS-02",
                "entity_type": "concept",
                "knowledge_title": title,
                "knowledge_lifecycle": "active",
                "evidence": {"present": True, "bytes": 86},
            }

        expected = copy.deepcopy(selected_before)
        expected["entities"].append(expected_entity)
        expected["entity_versions"].append(expected_version)
        expected["knowledge_entity_links"].extend(expected_links)
        for knowledge_id in (first, second):
            expected_document = next(
                row for row in expected["knowledge_objects"] if row["id"] == knowledge_id
            )
            expected_document.update(
                entity_id=entity_id, updated_at=links_by_document[knowledge_id]["created_at"]
            )
        same(actual, expected)
        for knowledge_id in (first, second):
            same(
                _bounded_public_knowledge_entity_links(app_storage, "alice", knowledge_id),
                [public_cards[knowledge_id]],
            )
        same(
            body,
            {
                "entity": expected_entity,
                "entity_created": True,
                "decided": 2,
                "skipped_existing": 0,
                "missing": 0,
                "decision": "accept",
            },
        )
        assert_audit(
            audit_before,
            decided,
            entity_id,
            {
                "name_chars": 8,
                "entity_type": "concept",
                "documents": 2,
                "skipped_existing": 0,
                "missing": 0,
                "entity_created": True,
            },
            start,
            finish,
        )

        # Повтор той же группы ничего не перезаписывает: решённое решено.
        replay_audits = audits()
        selected_after_first = rows()
        start = datetime.now(UTC)
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
        finish = datetime.now(UTC)
        assert replay.status_code == 200, replay.text
        assert replay.json()["decided"] == 0
        assert replay.json()["skipped_existing"] == 2
        reused_entity = {key: value for key, value in expected_entity.items() if key != "normalized_name"}
        reused_entity["metadata_json"] = "{}"
        same(
            replay.json(),
            {
                "entity": reused_entity,
                "entity_created": False,
                "decided": 0,
                "skipped_existing": 2,
                "missing": 0,
                "decision": "accept",
            },
        )
        same(rows(), selected_after_first)
        for knowledge_id in (first, second):
            same(
                _bounded_public_knowledge_entity_links(app_storage, "alice", knowledge_id),
                [public_cards[knowledge_id]],
            )
        assert_audit(
            replay_audits,
            replay,
            entity_id,
            {
                "name_chars": 8,
                "entity_type": "concept",
                "documents": 0,
                "skipped_existing": 2,
                "missing": 0,
                "entity_created": False,
            },
            start,
            finish,
        )


def test_group_reject_records_refusal_without_creating_a_node(settings, storage):
    """Отказ без узла — no-op; существующий узел получает только отклонённые связи."""
    import copy
    import json
    import re
    from dataclasses import replace
    from datetime import UTC, datetime, timedelta

    from fastapi.testclient import TestClient

    from friday.permissions import LEGACY_OWNER_USER_ID
    from friday.server import create_app

    def same(actual, expected):
        assert type(actual) is type(expected)
        if isinstance(expected, dict):
            assert actual.keys() == expected.keys()
            for key in expected:
                same(actual[key], expected[key])
        elif isinstance(expected, list):
            assert len(actual) == len(expected)
            for left, right in zip(actual, expected, strict=True):
                same(left, right)
        else:
            assert actual == expected

    def bounded_time(value, start, finish):
        assert isinstance(value, str)
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        assert parsed.tzinfo is not None and parsed.utcoffset() == timedelta(0)
        assert start.replace(microsecond=0) <= parsed <= finish
        return value

    def rows(app_storage):
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
                for row in app_storage.execute(
                    f"SELECT * FROM {table} WHERE user_id IN (?, ?) ORDER BY rowid",
                    ("alice", foreign),
                ).fetchall()
            ]
            for table in tables
        }
        selected["audit_log"] = [
            dict(row) for row in app_storage.execute("SELECT * FROM audit_log ORDER BY rowid").fetchall()
        ]
        return selected

    storage.ensure_user("alice")
    first = _document_with_text(storage, "alice", 5, "В тексте встречается в Наставлении по связи.")
    foreign = "entity-group-foreign-088"
    storage.ensure_user(foreign, preset_key="user")
    foreign_document = _document_with_text(storage, foreign, 105, "Чужой документ о Наставлении.")
    foreign_entity = Entity(
        id=new_id("ent"),
        user_id=foreign,
        name="Наставлении",
        entity_type=EntityType.LOCATION,
        metadata_json={"sentinel": "FOREIGN_GROUP_PRIVATE_CANARY"},
    )
    storage.create_entity(foreign_entity)
    storage.link_knowledge_entity(
        foreign,
        foreign_document,
        foreign_entity.id,
        status="accepted",
        reviewed_by=foreign,
        evidence={"sentinel": "FOREIGN_GROUP_PRIVATE_CANARY"},
    )
    # Every generated ID reaches its fixture writes in its native provenance type.
    first_id, foreign_document_id = str(first), str(foreign_document)
    foreign_entity_id = str(foreign_entity.id)

    with TestClient(create_app(replace(settings, shared_archive=False))) as client:
        headers = {"Authorization": f"Bearer {settings.api_token}"}
        app_storage = client.app.state.storage
        same(
            [
                dict(row)
                for row in app_storage.execute(
                    "SELECT id,user_id,name,entity_type FROM entities WHERE name=? ORDER BY id",
                    ("Наставлении",),
                ).fetchall()
            ],
            [
                {
                    "id": foreign_entity_id,
                    "user_id": foreign,
                    "name": "Наставлении",
                    "entity_type": "location",
                }
            ],
        )
        before = rows(app_storage)
        for document_id in (first_id, foreign_document_id):
            rejected = client.post(
                "/api/admin/entity-suggestions/groups/decide",
                json={
                    "user_id": "alice",
                    "name": "Наставлении",
                    "entity_type": "location",
                    "decision": "reject",
                    "knowledge_object_ids": [document_id],
                },
                headers=headers,
            )
            assert rejected.status_code == 200, rejected.text
            assert rejected.json()["entity"] is None
            assert rejected.json()["decided"] == 0
            assert client.app.state.storage.find_entity_by_name("alice", "Наставлении") is None
            same(
                rejected.json(),
                {
                    "entity": None,
                    "decided": 0,
                    "skipped_existing": 0,
                    "decision": "reject",
                },
            )
            same(rows(app_storage), before)
        invalid = client.post(
            "/api/admin/entity-suggestions/groups/decide",
            json={
                "user_id": "alice",
                "name": "Наставлении",
                "entity_type": "location",
                "decision": "invented",
                "knowledge_object_ids": [first_id],
            },
            headers=headers,
        )
        assert invalid.status_code == 400, invalid.text
        same(invalid.json(), {"detail": "decision должен быть accept или reject"})
        same(rows(app_storage), before)

        # A real own entity changes the branch. Reject two own documents, one with
        # no primary pointer and one already pointing at a different entity.
        second = _document_with_text(app_storage, "alice", 6, "Второй документ о Наставлении.")
        own_entity = Entity(
            id=new_id("ent"),
            user_id="alice",
            name="Наставлении",
            entity_type=EntityType.LOCATION,
            aliases_json=["Наставление"],
            description="Ранее созданная сущность.",
            metadata_json={"sentinel": "OWN_GROUP_PRIVATE_CANARY"},
        )
        app_storage.create_entity(own_entity)
        primary_entity = Entity(
            id=new_id("ent"),
            user_id="alice",
            name="Существующая основная связь",
            entity_type=EntityType.CONCEPT,
        )
        app_storage.create_entity(primary_entity)
        app_storage.link_knowledge_entity(
            "alice",
            second,
            primary_entity.id,
            status="accepted",
            reviewed_by="alice",
            evidence={"sentinel": "OWN_PRIMARY_PRIVATE_CANARY"},
        )
        own_id, second_id, primary_id = str(own_entity.id), str(second), str(primary_entity.id)
        entity_projection = {
            "id": own_id,
            "user_id": "alice",
            "name": "Наставлении",
            "entity_type": "location",
            "aliases_json": '["Наставление"]',
            "description": "Ранее созданная сущность.",
            "metadata_json": "{}",
            "canonical": 1,
            "merged_into_id": None,
            "version": 1,
            "created_at": own_entity.created_at,
            "updated_at": own_entity.updated_at,
            "deleted_at": None,
        }
        bulk_evidence = (
            '{"accepted_by": "964e5f17-a4bf-5744-a5c6-b7bfbdcd7bf0", "method": "human_review_bulk"}'
        )
        assert len(bulk_evidence.encode("utf-8")) == 86
        assert LEGACY_OWNER_USER_ID == "964e5f17-a4bf-5744-a5c6-b7bfbdcd7bf0"
        before = rows(app_storage)
        same(
            {row["id"]: row["entity_id"] for row in before["knowledge_objects"]},
            {
                first_id: None,
                second_id: primary_id,
                foreign_document_id: foreign_entity_id,
            },
        )
        request_body = {
            "user_id": "alice",
            "name": "Наставлении",
            "entity_type": "location",
            "decision": "reject",
            "knowledge_object_ids": [first_id, second_id, foreign_document_id],
        }
        # Replay must keep both rejected links and both primary pointers exact.
        for decided_count, skipped_count in ((2, 0), (0, 2)):
            started = datetime.now(UTC)
            rejected = client.post(
                "/api/admin/entity-suggestions/groups/decide",
                json=request_body,
                headers=headers,
            )
            finished = datetime.now(UTC)
            assert rejected.status_code == 200, rejected.text
            same(
                rejected.json(),
                {
                    "entity": entity_projection,
                    "entity_created": False,
                    "decided": decided_count,
                    "skipped_existing": skipped_count,
                    "missing": 1,
                    "decision": "reject",
                },
            )
            after = rows(app_storage)
            expected = copy.deepcopy(before)
            if decided_count == 2:
                old_links = before["knowledge_entity_links"]
                assert len(after["knowledge_entity_links"]) == len(old_links) + 2
                used_ids = {row["id"] for row in old_links}
                for index, document_id in enumerate((first_id, second_id)):
                    link = after["knowledge_entity_links"][len(old_links) + index]
                    link_id = link["id"]
                    assert isinstance(link_id, str) and re.fullmatch(r"kel_[0-9a-f]{16}", link_id)
                    assert link_id not in used_ids
                    used_ids.add(link_id)
                    stamp = bounded_time(link["created_at"], started, finished)
                    expected["knowledge_entity_links"].append(
                        {
                            "id": link_id,
                            "user_id": "alice",
                            "knowledge_object_id": document_id,
                            "entity_id": own_id,
                            "status": "rejected",
                            "confidence": 0.0,
                            "evidence_json": bulk_evidence,
                            "created_at": stamp,
                            "reviewed_at": stamp,
                            "reviewed_by": LEGACY_OWNER_USER_ID,
                        }
                    )
            assert len(after["audit_log"]) == len(before["audit_log"]) + 1
            audit = after["audit_log"][-1]
            audit_id = audit["id"]
            assert isinstance(audit_id, str) and re.fullmatch(r"audit_[0-9a-f]{16}", audit_id)
            assert audit_id not in {row["id"] for row in before["audit_log"]}
            request_id = rejected.headers["x-request-id"]
            assert re.fullmatch(r"[0-9a-f]{24}", request_id)
            expected["audit_log"].append(
                {
                    "id": audit_id,
                    "user_id": LEGACY_OWNER_USER_ID,
                    "action": "admin.entity_suggestion.bulk_reject",
                    "target_type": "entity",
                    "target_id": own_id,
                    "before_json": None,
                    "after_json": json.dumps(
                        {
                            "name_chars": 11,
                            "entity_type": "location",
                            "documents": decided_count,
                            "skipped_existing": skipped_count,
                            "missing": 1,
                            "entity_created": False,
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    "ip_address": "",
                    "request_id": request_id,
                    "created_at": bounded_time(audit["created_at"], started, finished),
                }
            )
            same(after, expected)
            assert "PRIVATE_CANARY" not in json.dumps(audit)
            before = after
