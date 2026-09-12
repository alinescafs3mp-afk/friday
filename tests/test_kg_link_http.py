"""Exact ordinary-user ``POST /api/kg/link`` HTTP contracts."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from friday.server import create_app
from friday.storage.models import Entity, KnowledgeObject, RawObject, new_id

P = "kg-link-person-p"
Q = "kg-link-person-q"
GUEST = "kg-link-guest"
STAMP = "2024-01-01T00:00:00+00:00"
P_TOKEN = "kg-link-p-token-" + "P" * 48
GUEST_TOKEN = "kg-link-guest-token-" + "G" * 48
CREATE_EVIDENCE = {"secret": "LINK_PRIVATE_CANARY"}
CREATE_EVIDENCE_JSON = '{"secret": "LINK_PRIVATE_CANARY"}'
UPSERT_EVIDENCE = {"secret": "LINK_PRIVATE_Ж"}
UPSERT_EVIDENCE_JSON = '{"secret": "LINK_PRIVATE_Ж"}'
TABLES = (
    "raw_objects",
    "knowledge_objects",
    "knowledge_object_versions",
    "entities",
    "entity_versions",
    "knowledge_entity_links",
)


def _same(actual, expected):
    assert type(actual) is type(expected)
    if isinstance(expected, dict):
        assert actual.keys() == expected.keys()
        for key in expected:
            _same(actual[key], expected[key])
    elif isinstance(expected, list):
        assert len(actual) == len(expected)
        for left, right in zip(actual, expected, strict=True):
            _same(left, right)
    else:
        assert actual == expected


def _id(value, prefix):
    assert type(value) is str
    assert re.fullmatch(prefix + r"_[0-9a-f]{16}", value)
    return value


def _time(value, start, finish):
    assert type(value) is str
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    assert parsed.tzinfo is not None and parsed.utcoffset() == timedelta(0)
    assert start.replace(microsecond=0) <= parsed <= finish
    return value


def _state(store):
    return {
        table: [
            dict(row)
            for row in store.execute(
                f"SELECT * FROM {table} WHERE user_id IN (?, ?) ORDER BY rowid", (P, Q)
            ).fetchall()
        ]
        for table in TABLES
    }


def _audits(store):
    return [dict(row) for row in store.execute("SELECT * FROM audit_log ORDER BY rowid").fetchall()]


def _audit_append(
    store,
    previous,
    response,
    *,
    user_id,
    action,
    target_type,
    target_id,
    start,
    finish,
    after,
    ip_address,
):
    rows = _audits(store)
    assert len(rows) == len(previous) + 1
    _same(rows[:-1], previous)
    row = rows[-1]
    audit_id = _id(row["id"], "audit")
    assert audit_id not in {old["id"] for old in previous}
    request_id = response.headers["x-request-id"]
    assert type(request_id) is str and re.fullmatch(r"[0-9a-f]{24}", request_id)
    _same(
        row,
        {
            "id": audit_id,
            "user_id": user_id,
            "action": action,
            "target_type": target_type,
            "target_id": target_id,
            "before_json": None,
            "after_json": json.dumps(after, ensure_ascii=False, sort_keys=True),
            "ip_address": ip_address,
            "request_id": request_id,
            "created_at": _time(row["created_at"], start, finish),
        },
    )
    return row


def _seed(store):
    started = datetime.now(UTC)
    for person, preset in ((P, "user"), (Q, "user"), (GUEST, "guest")):
        store.ensure_user(person, preset_key=preset)
    store.create_api_token(
        P,
        hashlib.sha256(P_TOKEN.encode()).hexdigest(),
        label="kg-link-p",
        created_by="test",
    )
    store.create_api_token(
        GUEST,
        hashlib.sha256(GUEST_TOKEN.encode()).hexdigest(),
        label="kg-link-guest",
        created_by="test",
    )

    fixture = {}
    for person, marker in ((P, "P"), (Q, "Q")):
        raw_id, ko_id, entity_id = new_id("raw"), new_id("ko"), new_id("ent")
        raw_content = f"{marker}_RAW_PRIVATE_CANARY"
        store.store_raw_object(
            RawObject(
                id=raw_id,
                user_id=person,
                source="upload",
                source_ref=f"kg-link-{marker.casefold()}",
                raw_content=raw_content,
                content_type="text",
                metadata_json={"fixture": f"{marker}_RAW_METADATA_CANARY"},
                content_hash=hashlib.sha256(raw_content.encode()).hexdigest(),
                received_at=STAMP,
                created_at=STAMP,
            )
        )
        store.store_knowledge_object(
            KnowledgeObject(
                id=ko_id,
                user_id=person,
                raw_object_id=raw_id,
                content=f"{marker}_KNOWLEDGE_PRIVATE_CANARY",
                content_type="text",
                title="Owner note",
                metadata_json={"fixture": f"{marker}_KNOWLEDGE_METADATA_CANARY"},
                created_at=STAMP,
                updated_at=STAMP,
            )
        )
        store.create_entity(
            Entity(
                id=entity_id,
                user_id=person,
                name="Link target",
                entity_type="concept",
                metadata_json={"fixture": f"{marker}_ENTITY_METADATA_CANARY"},
                created_at=STAMP,
                updated_at=STAMP,
            )
        )
        fixture[person] = {
            "raw": raw_id,
            "ko": ko_id,
            "entity": entity_id,
            "marker": marker,
        }

    foreign_link = new_id("kel")
    with store.transaction() as conn:
        conn.execute(
            """INSERT INTO knowledge_entity_links(
                   id, user_id, knowledge_object_id, entity_id, status, confidence,
                   evidence_json, created_at, reviewed_at, reviewed_by
               ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                foreign_link,
                Q,
                fixture[Q]["ko"],
                fixture[Q]["entity"],
                "rejected",
                0.6,
                '{"secret": "FOREIGN"}',
                STAMP,
                STAMP,
                Q,
            ),
        )
    finished = datetime.now(UTC)

    for values in fixture.values():
        values["raw"] = str(values["raw"])
        values["ko"] = str(values["ko"])
        values["entity"] = str(values["entity"])
    fixture["foreign_link"] = str(foreign_link)
    fixture["seed_started"] = started
    fixture["seed_finished"] = finished
    return fixture


def _raw_row(person, ids, marker):
    content = f"{marker}_RAW_PRIVATE_CANARY"
    return {
        "id": ids["raw"],
        "user_id": person,
        "source": "upload",
        "source_ref": f"kg-link-{marker.casefold()}",
        "raw_content": content,
        "content_type": "text",
        "metadata_json": json.dumps(
            {"fixture": f"{marker}_RAW_METADATA_CANARY"}, ensure_ascii=False, sort_keys=True
        ),
        "content_hash": hashlib.sha256(content.encode()).hexdigest(),
        "version": 1,
        "received_at": STAMP,
        "created_at": STAMP,
        "deleted_at": None,
    }


def _knowledge_row(person, ids, marker):
    return {
        "id": ids["ko"],
        "user_id": person,
        "raw_object_id": ids["raw"],
        "entity_id": None,
        "content": f"{marker}_KNOWLEDGE_PRIVATE_CANARY",
        "content_type": "text",
        "title": "Owner note",
        "summary": "",
        "tags_json": "[]",
        "metadata_json": json.dumps(
            {"fixture": f"{marker}_KNOWLEDGE_METADATA_CANARY"},
            ensure_ascii=False,
            sort_keys=True,
        ),
        "knowledge_kind": "note",
        "importance": 0.5,
        "quality_score": 0.5,
        "promotion_score": 0.5,
        "lifecycle_stage": "active",
        "version": 1,
        "superseded_by_id": None,
        "created_at": STAMP,
        "updated_at": STAMP,
        "deleted_at": None,
    }


def _entity_row(person, ids, marker):
    return {
        "id": ids["entity"],
        "user_id": person,
        "name": "Link target",
        "normalized_name": "link target",
        "entity_type": "concept",
        "aliases_json": "[]",
        "description": "",
        "metadata_json": json.dumps(
            {"fixture": f"{marker}_ENTITY_METADATA_CANARY"}, ensure_ascii=False, sort_keys=True
        ),
        "canonical": 1,
        "merged_into_id": None,
        "version": 1,
        "created_at": STAMP,
        "updated_at": STAMP,
        "deleted_at": None,
    }


def _assert_seed(store, fixture):
    state = _state(store)
    assert {table: len(rows) for table, rows in state.items()} == {
        "raw_objects": 2,
        "knowledge_objects": 2,
        "knowledge_object_versions": 2,
        "entities": 2,
        "entity_versions": 2,
        "knowledge_entity_links": 1,
    }
    expected_raw = []
    expected_knowledge = []
    expected_entities = []
    for person in (P, Q):
        ids, marker = fixture[person], fixture[person]["marker"]
        expected_raw.append(_raw_row(person, ids, marker))
        expected_knowledge.append(_knowledge_row(person, ids, marker))
        expected_entities.append(_entity_row(person, ids, marker))
    _same(state["raw_objects"], expected_raw)
    _same(state["knowledge_objects"], expected_knowledge)
    _same(state["entities"], expected_entities)

    for row, person, expected in zip(
        state["knowledge_object_versions"], (P, Q), expected_knowledge, strict=True
    ):
        version_id = _id(row["id"], "kov")
        _same(
            row,
            {
                "id": version_id,
                "user_id": person,
                "knowledge_object_id": fixture[person]["ko"],
                "version": 1,
                "snapshot_json": json.dumps(expected, ensure_ascii=False, sort_keys=True),
                "created_at": _time(row["created_at"], fixture["seed_started"], fixture["seed_finished"]),
            },
        )
    for row, person, expected in zip(state["entity_versions"], (P, Q), expected_entities, strict=True):
        version_id = _id(row["id"], "entv")
        _same(
            row,
            {
                "id": version_id,
                "user_id": person,
                "entity_id": fixture[person]["entity"],
                "version": 1,
                "snapshot_json": json.dumps(expected, ensure_ascii=False, sort_keys=True),
                "created_at": _time(row["created_at"], fixture["seed_started"], fixture["seed_finished"]),
            },
        )
    _same(
        state["knowledge_entity_links"],
        [
            {
                "id": fixture["foreign_link"],
                "user_id": Q,
                "knowledge_object_id": fixture[Q]["ko"],
                "entity_id": fixture[Q]["entity"],
                "status": "rejected",
                "confidence": 0.6,
                "evidence_json": '{"secret": "FOREIGN"}',
                "created_at": STAMP,
                "reviewed_at": STAMP,
                "reviewed_by": Q,
            }
        ],
    )
    assert not any(row["user_id"] == P for row in state["knowledge_entity_links"])


def _private_canaries_absent(text):
    for canary in (
        "LINK_PRIVATE_CANARY",
        "LINK_PRIVATE_Ж",
        "P_RAW_PRIVATE_CANARY",
        "Q_RAW_PRIVATE_CANARY",
        "P_RAW_METADATA_CANARY",
        "Q_RAW_METADATA_CANARY",
        "P_KNOWLEDGE_PRIVATE_CANARY",
        "Q_KNOWLEDGE_PRIVATE_CANARY",
        "P_KNOWLEDGE_METADATA_CANARY",
        "Q_KNOWLEDGE_METADATA_CANARY",
        "P_ENTITY_METADATA_CANARY",
        "Q_ENTITY_METADATA_CANARY",
        '"evidence_json"',
        '"reviewed_by"',
        '"user_id"',
    ):
        assert canary not in text


@pytest.fixture
def kg_link_pair(settings):
    app_settings = replace(
        settings,
        shared_archive=False,
        api_require_token_on_loopback=True,
    )
    with TestClient(create_app(app_settings), raise_server_exceptions=False) as client:
        store = client.app.state.storage
        fixture = _seed(store)
        _assert_seed(store, fixture)
        yield client, store, {"Authorization": f"Bearer {P_TOKEN}"}, fixture


def test_kg_link_creates_then_upserts_exact_public_raw_and_audit(kg_link_pair):
    client, store, headers, fixture = kg_link_pair
    ko_id, entity_id = fixture[P]["ko"], fixture[P]["entity"]
    before = _state(store)
    audits = _audits(store)
    start = datetime.now(UTC)
    response = client.post(
        "/api/kg/link",
        headers={**headers, "Origin": "https://third-party.example"},
        json={
            "knowledge_object_id": ko_id,
            "entity_id": entity_id,
            "evidence": CREATE_EVIDENCE,
        },
    )
    finish = datetime.now(UTC)
    assert response.status_code == 200, response.text
    card = response.json()["link"]
    link_id = _id(card["id"], "kel")
    assert link_id not in {row["id"] for row in before["knowledge_entity_links"]}
    created_at = _time(card["created_at"], start, finish)
    reviewed_at = _time(card["reviewed_at"], start, finish)
    assert created_at == reviewed_at
    expected_card = {
        "id": link_id,
        "knowledge_object_id": ko_id,
        "entity_id": entity_id,
        "status": "accepted",
        "confidence": 1.0,
        "created_at": created_at,
        "reviewed_at": reviewed_at,
        "entity_name": "Link target",
        "entity_type": "concept",
        "knowledge_title": "Owner note",
        "knowledge_lifecycle": "active",
        "evidence": {"present": True, "bytes": 33},
    }
    _same(response.json(), {"link": expected_card})
    _private_canaries_absent(response.text)

    expected = copy.deepcopy(before)
    expected["knowledge_entity_links"].append(
        {
            "id": link_id,
            "user_id": P,
            "knowledge_object_id": ko_id,
            "entity_id": entity_id,
            "status": "accepted",
            "confidence": 1.0,
            "evidence_json": CREATE_EVIDENCE_JSON,
            "created_at": created_at,
            "reviewed_at": reviewed_at,
            "reviewed_by": P,
        }
    )
    knowledge = next(row for row in expected["knowledge_objects"] if row["id"] == ko_id)
    assert knowledge["entity_id"] is None and knowledge["updated_at"] == STAMP
    knowledge.update(entity_id=entity_id, updated_at=created_at)
    _same(_state(store), expected)
    audit_after = {
        "id": link_id,
        "knowledge_object_id": ko_id,
        "entity_id": entity_id,
        "status": "accepted",
        "confidence": 1.0,
        "created_at": created_at,
        "reviewed_at": reviewed_at,
        "entity_type": "concept",
        "private_fields_count": 4,
        "private_chars": 27,
        "private_items_count": 2,
    }
    audit = _audit_append(
        store,
        audits,
        response,
        user_id=P,
        action="knowledge.entity_link",
        target_type="knowledge_entity_link",
        target_id=link_id,
        start=start,
        finish=finish,
        after=audit_after,
        ip_address="",
    )
    _private_canaries_absent(audit["after_json"])
    assert "Link target" not in audit["after_json"]
    assert "Owner note" not in audit["after_json"]

    before = _state(store)
    audits = _audits(store)
    first_knowledge_updated_at = next(
        row["updated_at"] for row in before["knowledge_objects"] if row["id"] == ko_id
    )
    start = datetime.now(UTC)
    response = client.post(
        "/api/kg/link",
        headers=headers,
        json={
            "knowledge_object_id": ko_id,
            "entity_id": entity_id,
            "status": "suggested",
            "confidence": 0.25,
            "evidence": UPSERT_EVIDENCE,
        },
    )
    finish = datetime.now(UTC)
    assert response.status_code == 200, response.text
    second_card = response.json()["link"]
    second_reviewed_at = _time(second_card["reviewed_at"], start, finish)
    expected_second_card = dict(expected_card)
    expected_second_card.update(
        status="suggested",
        confidence=0.25,
        reviewed_at=second_reviewed_at,
        evidence={"present": True, "bytes": 29},
    )
    _same(response.json(), {"link": expected_second_card})
    _private_canaries_absent(response.text)

    expected = copy.deepcopy(before)
    assert len(expected["knowledge_entity_links"]) == 2
    p_link = next(row for row in expected["knowledge_entity_links"] if row["user_id"] == P)
    assert p_link["id"] == link_id and p_link["created_at"] == created_at
    p_link.update(
        status="suggested",
        confidence=0.25,
        evidence_json=UPSERT_EVIDENCE_JSON,
        reviewed_at=second_reviewed_at,
        reviewed_by=P,
    )
    p_knowledge = next(row for row in expected["knowledge_objects"] if row["id"] == ko_id)
    assert p_knowledge["entity_id"] == entity_id
    assert p_knowledge["updated_at"] == first_knowledge_updated_at
    _same(_state(store), expected)
    second_audit_after = dict(audit_after)
    second_audit_after.update(status="suggested", confidence=0.25, reviewed_at=second_reviewed_at)
    audit = _audit_append(
        store,
        audits,
        response,
        user_id=P,
        action="knowledge.entity_link",
        target_type="knowledge_entity_link",
        target_id=link_id,
        start=start,
        finish=finish,
        after=second_audit_after,
        ip_address="",
    )
    _private_canaries_absent(audit["after_json"])
    assert "Link target" not in audit["after_json"]
    assert "Owner note" not in audit["after_json"]


def test_kg_link_invalid_and_foreign_inputs_preserve_selected_rows(kg_link_pair):
    client, store, headers, fixture = kg_link_pair
    valid = {
        "knowledge_object_id": fixture[P]["ko"],
        "entity_id": fixture[P]["entity"],
        "evidence": CREATE_EVIDENCE,
    }
    missing_ko = "ko_0000000000000000"
    missing_entity = "ent_0000000000000000"
    baseline = _state(store)
    assert missing_ko not in {row["id"] for row in baseline["knowledge_objects"]}
    assert missing_entity not in {row["id"] for row in baseline["entities"]}
    cases = (
        (
            {"knowledge_object_id": missing_ko},
            "Knowledge object and entity must belong to the same user",
        ),
        (
            {"entity_id": missing_entity},
            "Knowledge object and entity must belong to the same user",
        ),
        (
            {"knowledge_object_id": fixture[Q]["ko"]},
            "Knowledge object and entity must belong to the same user",
        ),
        (
            {"entity_id": fixture[Q]["entity"]},
            "Knowledge object and entity must belong to the same user",
        ),
        ({"status": "invented"}, "status must be suggested, accepted, or rejected"),
        ({"confidence": True}, "confidence: нужно число"),
        ({"confidence": "NaN"}, "confidence: нужно конечное число"),
        ({"confidence": -0.1}, "confidence: значение от 0 до 1"),
        ({"confidence": 1.1}, "confidence: значение от 0 до 1"),
    )
    for replacement, detail in cases:
        payload = copy.deepcopy(valid)
        payload.update(replacement)
        before, audits = _state(store), _audits(store)
        response = client.post("/api/kg/link", headers=headers, json=payload)
        assert response.status_code == 400, response.text
        _same(response.json(), {"detail": detail})
        _same(_state(store), before)
        _same(_audits(store), audits)
    _same(_state(store), baseline)


def test_kg_link_auth_and_loopback_csrf_refusals_have_exact_audit_effects(settings):
    required = replace(
        settings,
        shared_archive=False,
        api_require_token_on_loopback=True,
    )
    valid = None
    with TestClient(
        create_app(required),
        client=("127.0.0.1", 9000),
        base_url="http://127.0.0.1:8000",
        raise_server_exceptions=False,
    ) as client:
        store = client.app.state.storage
        fixture = _seed(store)
        _assert_seed(store, fixture)
        valid = {
            "knowledge_object_id": fixture[P]["ko"],
            "entity_id": fixture[P]["entity"],
            "evidence": CREATE_EVIDENCE,
        }
        before, audits = _state(store), _audits(store)
        start = datetime.now(UTC)
        response = client.post("/api/kg/link", json=valid)
        finish = datetime.now(UTC)
        assert response.status_code == 401, response.text
        _same(response.json(), {"detail": "Missing authentication"})
        _same(_state(store), before)
        _audit_append(
            store,
            audits,
            response,
            user_id="anonymous",
            action="auth.failed",
            target_type="auth",
            target_id="invalid_credentials",
            start=start,
            finish=finish,
            after={
                "status_present": True,
                "reason": "invalid_credentials",
                "method_chars": 4,
                "path_chars": 12,
            },
            ip_address="127.0.0.1",
        )

        before, audits = _state(store), _audits(store)
        response = client.post(
            "/api/kg/link",
            json=valid,
            headers={"Authorization": f"Bearer {GUEST_TOKEN}"},
        )
        assert response.status_code == 403, response.text
        _same(response.json(), {"detail": "Access denied for kg.write (default_deny)"})
        _same(_state(store), before)
        _same(_audits(store), audits)

    optional = replace(required, api_require_token_on_loopback=False)
    with TestClient(
        create_app(optional),
        client=("127.0.0.1", 9000),
        base_url="http://127.0.0.1:8000",
        raise_server_exceptions=False,
    ) as client:
        store = client.app.state.storage
        before, audits = _state(store), _audits(store)
        start = datetime.now(UTC)
        response = client.post(
            "/api/kg/link",
            json=valid,
            headers={"Origin": "https://evil.example"},
        )
        finish = datetime.now(UTC)
        assert response.status_code == 403, response.text
        _same(
            response.json(),
            {"detail": "Cross-origin browser requests cannot use loopback authentication"},
        )
        _same(_state(store), before)
        _audit_append(
            store,
            audits,
            response,
            user_id="anonymous",
            action="auth.failed",
            target_type="auth",
            target_id="capability_denied",
            start=start,
            finish=finish,
            after={
                "status_present": True,
                "reason_chars": 17,
                "method_chars": 4,
                "path_chars": 12,
            },
            ip_address="127.0.0.1",
        )
