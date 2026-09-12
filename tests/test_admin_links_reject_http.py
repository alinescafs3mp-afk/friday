"""Exact admin reject/link POST contracts; real storage, selected-row deltas."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from friday.permissions import LEGACY_OWNER_USER_ID
from friday.server import create_app
from friday.storage.models import Entity, EntityResolutionCandidate, KnowledgeObject, RawObject, new_id

P = "admin-links-person-p"
Q = "admin-links-person-q"
STAMP = "2024-01-01T00:00:00+00:00"
EVIDENCE = {"secret": "LINK_PRIVATE_CANARY"}
EVIDENCE_JSON = '{"secret": "LINK_PRIVATE_CANARY"}'
TABLES = (
    "entities",
    "entity_versions",
    "raw_objects",
    "knowledge_objects",
    "knowledge_entity_links",
    "entity_resolution_candidates",
    "relations",
    "entity_merge_history",
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
    assert isinstance(value, str) and re.fullmatch(prefix + r"_[0-9a-f]{16}", value)
    return value


def _time(value, start, finish):
    assert isinstance(value, str)
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    assert parsed.tzinfo is not None and parsed.utcoffset() == timedelta(0)
    assert start.replace(microsecond=0) <= parsed <= finish
    return value


def _state(store):
    # Only these eight tables and the two synthetic tenants are asserted.
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


def _audit_append(store, previous, response, action, target_type, target_id, start, finish, after=None):
    rows = _audits(store)
    assert len(rows) == len(previous) + 1
    _same(rows[:-1], previous)
    row = rows[-1]
    audit_id = _id(row["id"], "audit")
    assert audit_id not in {old["id"] for old in previous}
    request_id = response.headers["x-request-id"]
    assert re.fullmatch(r"[0-9a-f]{24}", request_id)
    _same(
        row,
        {
            "id": audit_id,
            "user_id": LEGACY_OWNER_USER_ID,
            "action": action,
            "target_type": target_type,
            "target_id": target_id,
            "before_json": None,
            "after_json": json.dumps(after, ensure_ascii=False, sort_keys=True)
            if after is not None
            else None,
            "ip_address": "",
            "request_id": request_id,
            "created_at": _time(row["created_at"], start, finish),
        },
    )
    assert "LINK_PRIVATE_CANARY" not in json.dumps(rows[-1])


@pytest.fixture
def admin_pair(settings):
    with TestClient(
        create_app(replace(settings, shared_archive=False)), raise_server_exceptions=False
    ) as client:
        store = client.app.state.storage
        fixture = {}
        for person, title in ((P, "Owner note"), (Q, "Foreign note")):
            store.ensure_user(person, preset_key="user")
            left, right = _id(new_id("ent"), "ent"), _id(new_id("ent"), "ent")
            for entity_id, name in ((left, "Link target"), (right, "Other target")):
                store.create_entity(
                    Entity(
                        id=entity_id,
                        user_id=person,
                        name=name,
                        entity_type="concept",
                        created_at=STAMP,
                        updated_at=STAMP,
                        metadata_json={"fixture": "PRIVATE_ENTITY_CANARY"},
                    )
                )
            raw_id, ko_id, candidate_id = new_id("raw"), new_id("ko"), new_id("er")
            store.store_raw_object(
                RawObject(
                    id=raw_id,
                    user_id=person,
                    source="upload",
                    source_ref="admin-link-fixture",
                    raw_content="Synthetic note",
                    content_type="text",
                    content_hash=hashlib.sha256(b"Synthetic note").hexdigest(),
                    received_at=STAMP,
                    created_at=STAMP,
                )
            )
            store.store_knowledge_object(
                KnowledgeObject(
                    id=ko_id,
                    user_id=person,
                    raw_object_id=raw_id,
                    content="Synthetic note",
                    content_type="text",
                    title=title,
                    created_at=STAMP,
                    updated_at=STAMP,
                )
            )
            store.store_resolution_candidate(
                EntityResolutionCandidate(
                    id=candidate_id,
                    user_id=person,
                    entity_a_id=left,
                    entity_b_id=right,
                    confidence=0.75,
                    resolution_method="name_similarity",
                    created_at=STAMP,
                    evidence_json={"note": "PRIVATE_RESOLUTION_CANARY"},
                )
            )
            # Store writes above retain generated-ID provenance. SQL/JSON
            # projections below expose plain strings, as do the HTTP fixtures.
            fixture[person] = {
                "left": str(left),
                "right": str(right),
                "ko": str(ko_id),
                "candidate": str(candidate_id),
            }
            candidate = dict(
                store.execute(
                    "SELECT * FROM entity_resolution_candidates WHERE id=?", (candidate_id,)
                ).fetchone()
            )
            _same(
                candidate,
                {
                    "id": str(candidate_id),
                    "user_id": person,
                    "entity_a_id": str(left),
                    "entity_b_id": str(right),
                    "pair_key": "|".join(sorted((left, right))),
                    "confidence": 0.75,
                    "resolution_method": "name_similarity",
                    "evidence_json": '{"note": "PRIVATE_RESOLUTION_CANARY"}',
                    "status": "suggested",
                    "resolved_by": None,
                    "created_at": STAMP,
                    "resolved_at": None,
                },
            )
        assert _state(store)["knowledge_entity_links"] == []
        yield client, store, {"Authorization": f"Bearer {settings.api_token}"}, fixture


def test_admin_reject_changes_only_candidate_decision_and_replays(admin_pair):
    client, store, headers, fixture = admin_pair
    candidate_id = fixture[P]["candidate"]
    for attempt in range(2):
        before, audits = _state(store), _audits(store)
        start = datetime.now(UTC)
        response = client.post(
            f"/api/admin/resolutions/{candidate_id}/reject", json={"user_id": P}, headers=headers
        )
        finish = datetime.now(UTC)
        assert response.status_code == 200, response.text
        _same(response.json(), {"status": "rejected"})
        actual = _state(store)
        changed = next(row for row in actual["entity_resolution_candidates"] if row["id"] == candidate_id)
        resolved_at = _time(changed["resolved_at"], start, finish)
        expected = copy.deepcopy(before)
        target = next(row for row in expected["entity_resolution_candidates"] if row["id"] == candidate_id)
        assert target["status"] == ("suggested" if attempt == 0 else "rejected")
        target.update(status="rejected", resolved_by=LEGACY_OWNER_USER_ID, resolved_at=resolved_at)
        _same(actual, expected)
        _audit_append(
            store, audits, response, "admin.entity.merge_rejected", "resolution", candidate_id, start, finish
        )


@pytest.mark.parametrize("which", ["missing", "foreign"], ids=["missing", "foreign"])
def test_admin_reject_missing_and_foreign_are_404_without_selected_effects(admin_pair, which):
    client, store, headers, fixture = admin_pair
    candidate = "er_0000000000000000" if which == "missing" else fixture[Q]["candidate"]
    before, audits = _state(store), _audits(store)
    response = client.post(f"/api/admin/resolutions/{candidate}/reject", json={"user_id": P}, headers=headers)
    assert response.status_code == 404, response.text
    _same(response.json(), {"detail": "Кандидат на объединение не найден"})
    _same(_state(store), before)
    _same(_audits(store), audits)


def test_admin_reject_unexpected_resolver_error_stays_internal_without_selected_effects(
    admin_pair, monkeypatch
):
    client, store, headers, fixture = admin_pair
    before, audits = _state(store), _audits(store)

    def fail_unexpectedly(*args, **kwargs):
        raise ValueError("PRIVATE_UNEXPECTED_RESOLVER_FAILURE")

    monkeypatch.setattr(client.app.state.kg.resolver, "reject_resolution", fail_unexpectedly)
    response = client.post(
        f"/api/admin/resolutions/{fixture[P]['candidate']}/reject", json={"user_id": P}, headers=headers
    )
    assert response.status_code == 500
    assert "PRIVATE_UNEXPECTED_RESOLVER_FAILURE" not in response.text
    _same(_state(store), before)
    _same(_audits(store), audits)


def test_admin_entity_link_create_and_upsert_pin_public_storage_and_audit(admin_pair):
    client, store, headers, fixture = admin_pair
    entity_id, ko_id = fixture[P]["left"], fixture[P]["ko"]
    created_at = link_id = None
    for status, confidence in (("accepted", 0.75), ("suggested", 0.25)):
        before, audits = _state(store), _audits(store)
        start = datetime.now(UTC)
        response = client.post(
            f"/api/admin/knowledge/{ko_id}/entity-links",
            json={
                "user_id": P,
                "entity_id": entity_id,
                "status": status,
                "confidence": confidence,
                "evidence": EVIDENCE,
            },
            headers=headers,
        )
        finish = datetime.now(UTC)
        assert response.status_code == 200, response.text
        card = response.json()["link"]
        reviewed_at = _time(card["reviewed_at"], start, finish)
        if link_id is None:
            link_id = _id(card["id"], "kel")
            assert link_id not in {row["id"] for row in before["knowledge_entity_links"]}
            created_at = _time(card["created_at"], start, finish)
            assert created_at == reviewed_at
        _same(
            response.json(),
            {
                "link": {
                    "id": link_id,
                    "knowledge_object_id": ko_id,
                    "entity_id": entity_id,
                    "status": status,
                    "confidence": confidence,
                    "created_at": created_at,
                    "reviewed_at": reviewed_at,
                    "entity_name": "Link target",
                    "entity_type": "concept",
                    "knowledge_title": "Owner note",
                    "knowledge_lifecycle": "active",
                    "evidence": {"present": True, "bytes": 33},
                }
            },
        )
        expected = copy.deepcopy(before)
        row = {
            "id": link_id,
            "user_id": P,
            "knowledge_object_id": ko_id,
            "entity_id": entity_id,
            "status": status,
            "confidence": confidence,
            "evidence_json": EVIDENCE_JSON,
            "created_at": created_at,
            "reviewed_at": reviewed_at,
            "reviewed_by": LEGACY_OWNER_USER_ID,
        }
        if status == "accepted":
            expected["knowledge_entity_links"].append(row)
            ko = next(item for item in expected["knowledge_objects"] if item["id"] == ko_id)
            assert ko["entity_id"] is None
            ko.update(entity_id=entity_id, updated_at=created_at)
        else:
            assert len(expected["knowledge_entity_links"]) == 1
            expected["knowledge_entity_links"][0] = row
        _same(_state(store), expected)
        # Literal audited schema: names/lifecycle/evidence are hidden, not copied
        # through the product sanitizer to manufacture the expected projection.
        audit_after = {
            "id": link_id,
            "knowledge_object_id": ko_id,
            "entity_id": entity_id,
            "status": status,
            "confidence": confidence,
            "created_at": created_at,
            "reviewed_at": reviewed_at,
            "entity_type": "concept",
            "private_fields_count": 4,
            "private_chars": 27,
            "private_items_count": 2,
        }
        _audit_append(
            store,
            audits,
            response,
            "admin.knowledge.entity_link.create",
            "knowledge_entity_link",
            link_id,
            start,
            finish,
            after=audit_after,
        )
        assert "LINK_PRIVATE_CANARY" not in response.text


@pytest.mark.parametrize(
    "case, detail",
    [
        ("missing-knowledge", "Knowledge object and entity must belong to the same user"),
        ("missing-entity", "Knowledge object and entity must belong to the same user"),
        ("foreign-knowledge", "Knowledge object and entity must belong to the same user"),
        ("foreign-entity", "Knowledge object and entity must belong to the same user"),
        ("invalid-status", "status должен быть suggested, accepted или rejected"),
        ("invalid-confidence", "confidence: нужно конечное число"),
    ],
    ids=[
        "missing-knowledge",
        "missing-entity",
        "foreign-knowledge",
        "foreign-entity",
        "invalid-status",
        "invalid-confidence",
    ],
)
def test_admin_entity_link_refusals_are_400_without_selected_effects(admin_pair, case, detail):
    client, store, headers, fixture = admin_pair
    ko_id, entity_id = fixture[P]["ko"], fixture[P]["left"]
    if case == "missing-knowledge":
        ko_id = "ko_0000000000000000"
    if case == "missing-entity":
        entity_id = "ent_0000000000000000"
    if case == "foreign-knowledge":
        ko_id = fixture[Q]["ko"]
    if case == "foreign-entity":
        entity_id = fixture[Q]["left"]
    payload = {"user_id": P, "entity_id": entity_id, "evidence": EVIDENCE}
    if case == "invalid-status":
        payload["status"] = "invented"
    if case == "invalid-confidence":
        payload["confidence"] = "NaN"
    before, audits = _state(store), _audits(store)
    response = client.post(f"/api/admin/knowledge/{ko_id}/entity-links", json=payload, headers=headers)
    assert response.status_code == 400, response.text
    _same(response.json(), {"detail": detail})
    _same(_state(store), before)
    _same(_audits(store), audits)
