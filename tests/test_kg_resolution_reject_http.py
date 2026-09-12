"""K3 reject HTTP decisions: exact selected state and attributed audit."""

from __future__ import annotations

import copy
import hashlib
import re
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from friday.server import create_app
from friday.storage.models import (
    Entity,
    EntityResolutionCandidate,
    KnowledgeObject,
    RawObject,
    new_id,
)

MODERATOR = "k3-reject-moderator"
ADMIN = "k3-reject-admin"
USER = "k3-reject-user"
GUEST = "k3-reject-guest"
FOREIGN = "k3-reject-foreign"
PEOPLE = (MODERATOR, ADMIN, USER, GUEST, FOREIGN)
STAMP = "2024-01-01T00:00:00+00:00"
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


def _native_id(prefix):
    # Keep the generated-ID subclass until every managed storage write finishes.
    value = new_id(prefix)
    assert isinstance(value, str)
    assert re.fullmatch(prefix + r"_[0-9a-f]{16}", value)
    return value


def _plain_id(value, prefix):
    assert type(value) is str
    assert re.fullmatch(prefix + r"_[0-9a-f]{16}", value)
    return value


def _utc_in_window(value, start, finish):
    assert type(value) is str
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    assert parsed.tzinfo is not None and parsed.utcoffset() == timedelta(0)
    # Product utc_now has second precision; replay can retain the same second.
    assert start.replace(microsecond=0) <= parsed <= finish
    return value


def _state(store):
    # Full rows, limited to these eight tables and five synthetic tenants.
    return {
        table: [
            dict(row)
            for row in store.execute(
                f"SELECT * FROM {table} WHERE user_id IN (?, ?, ?, ?, ?) ORDER BY rowid",
                PEOPLE,
            ).fetchall()
        ]
        for table in TABLES
    }


def _audits(store):
    return [dict(row) for row in store.execute("SELECT * FROM audit_log ORDER BY rowid").fetchall()]


def _candidate_row(candidate_id, person, left, right, status="suggested"):
    # This oracle uses fixture inputs, never resolver output or model.to_row().
    return {
        "id": str(candidate_id),
        "user_id": person,
        "entity_a_id": str(left),
        "entity_b_id": str(right),
        "pair_key": "|".join(sorted((str(left), str(right)))),
        "confidence": 0.75,
        "resolution_method": "name_similarity",
        "evidence_json": '{"note": "K3_PRIVATE_RESOLUTION_CANARY"}',
        "status": status,
        "resolved_by": person if status == "merged" else None,
        "created_at": STAMP,
        "resolved_at": STAMP if status == "merged" else None,
    }


def _seed_pair(store, person, label, *, status="suggested"):
    left, right = _native_id("ent"), _native_id("ent")
    for entity_id, name in ((left, label + " left"), (right, label + " right")):
        store.create_entity(
            Entity(
                id=entity_id,
                user_id=person,
                name=name,
                entity_type="concept",
                metadata_json={"fixture": "K3_ENTITY_CANARY"},
                created_at=STAMP,
                updated_at=STAMP,
            )
        )
    raw_id, knowledge_id = _native_id("raw"), _native_id("ko")
    content = "K3 synthetic note " + label
    store.store_raw_object(
        RawObject(
            id=raw_id,
            user_id=person,
            source="upload",
            source_ref=f"k3-reject-fixture:{label}",
            raw_content=content,
            content_type="text",
            content_hash=hashlib.sha256(content.encode()).hexdigest(),
            received_at=STAMP,
            created_at=STAMP,
        )
    )
    store.store_knowledge_object(
        KnowledgeObject(
            id=knowledge_id,
            user_id=person,
            raw_object_id=raw_id,
            content=content,
            content_type="text",
            title=label,
            created_at=STAMP,
            updated_at=STAMP,
        )
    )
    store.link_knowledge_entity(
        person,
        knowledge_id,
        left,
        status="accepted",
        confidence=0.625,
        evidence={"note": "K3_LINK_CANARY"},
        reviewed_by=person,
    )
    candidate_id = _native_id("er")
    store.store_resolution_candidate(
        EntityResolutionCandidate(
            id=candidate_id,
            user_id=person,
            entity_a_id=left,
            entity_b_id=right,
            confidence=0.75,
            resolution_method="name_similarity",
            evidence_json={"note": "K3_PRIVATE_RESOLUTION_CANARY"},
            status=status,
            resolved_by=person if status == "merged" else None,
            created_at=STAMP,
            resolved_at=STAMP if status == "merged" else None,
        )
    )
    expected = _candidate_row(candidate_id, person, left, right, status)
    row = store.execute("SELECT * FROM entity_resolution_candidates WHERE id=?", (candidate_id,)).fetchone()
    assert row is not None
    _same(dict(row), expected)
    knowledge = store.execute(
        "SELECT entity_id FROM knowledge_objects WHERE id=?", (knowledge_id,)
    ).fetchone()
    assert knowledge is not None
    _same(dict(knowledge), {"entity_id": str(left)})
    # Generated IDs become plain expected values only after native writes.
    return {
        "candidate": str(candidate_id),
        "left": str(left),
        "right": str(right),
        "knowledge": str(knowledge_id),
        "initial_candidate": expected,
    }


@pytest.fixture
def kg_reject_case(settings):
    app = create_app(replace(settings, shared_archive=False, api_require_token_on_loopback=True))
    with TestClient(app, raise_server_exceptions=False) as client:
        store = app.state.storage
        headers, pairs = {}, {}
        for person, preset in (
            (MODERATOR, "moderator"),
            (ADMIN, "admin"),
            (USER, "user"),
            (GUEST, "guest"),
            (FOREIGN, "user"),
        ):
            store.ensure_user(person, source="api-token", display_name=person, preset_key=preset)
            store.update_user(person, preset_key=preset)
            secret = "k3-token-" + person + "-" + "S" * 40
            store.create_api_token(
                person,
                hashlib.sha256(secret.encode()).hexdigest(),
                label="k3-reject-http",
                created_by="fixture",
            )
            headers[person] = {"Authorization": f"Bearer {secret}"}
            pairs[person] = _seed_pair(store, person, person)
        # Persist the terminal status through a managed candidate insert. This
        # tests its refusal branch; it does not simulate a completed merge.
        merged = _seed_pair(store, MODERATOR, "terminal pair", status="merged")
        state = _state(store)
        _same(
            {table: len(rows) for table, rows in state.items()},
            {
                "entities": 12,
                "entity_versions": 12,
                "raw_objects": 6,
                "knowledge_objects": 6,
                "knowledge_entity_links": 6,
                "entity_resolution_candidates": 6,
                "relations": 0,
                "entity_merge_history": 0,
            },
        )
        assert len({row["id"] for row in state["entity_resolution_candidates"]}) == 6
        yield client, store, headers, pairs, merged


def _audit_append(store, previous, response, actor_id, candidate_id, start, finish):
    rows = _audits(store)
    assert len(rows) == len(previous) + 1
    _same(rows[:-1], previous)
    row = rows[-1]
    audit_id = _plain_id(row["id"], "audit")
    assert audit_id not in {old["id"] for old in previous}
    request_id = response.headers["x-request-id"]
    assert type(request_id) is str and re.fullmatch(r"[0-9a-f]{24}", request_id)
    assert request_id not in {old["request_id"] for old in previous}
    _same(
        row,
        {
            "id": audit_id,
            "user_id": actor_id,
            "action": "entity.merge_rejected",
            "target_type": "resolution",
            "target_id": candidate_id,
            "before_json": None,
            "after_json": None,
            "ip_address": "",
            "request_id": request_id,
            "created_at": _utc_in_window(row["created_at"], start, finish),
        },
    )


def test_kg_reject_moderator_and_admin_change_only_decision_and_audit_replay(kg_reject_case):
    client, store, headers, pairs, _ = kg_reject_case
    for actor in (MODERATOR, ADMIN):
        candidate_id = _plain_id(pairs[actor]["candidate"], "er")
        for attempt in range(2):
            before, audits = _state(store), _audits(store)
            target_before = next(
                row for row in before["entity_resolution_candidates"] if row["id"] == candidate_id
            )
            if attempt == 0:
                _same(target_before, pairs[actor]["initial_candidate"])
            else:
                _same(target_before["status"], "rejected")
                _same(target_before["resolved_by"], actor)
            start = datetime.now(UTC)
            response = client.post(f"/api/kg/resolutions/{candidate_id}/reject", headers=headers[actor])
            finish = datetime.now(UTC)
            assert response.status_code == 200, response.text
            _same(response.json(), {"status": "rejected"})
            actual = _state(store)
            changed = next(row for row in actual["entity_resolution_candidates"] if row["id"] == candidate_id)
            resolved_at = _utc_in_window(changed["resolved_at"], start, finish)
            expected = copy.deepcopy(before)
            target = next(
                row for row in expected["entity_resolution_candidates"] if row["id"] == candidate_id
            )
            target.update(status="rejected", resolved_by=actor, resolved_at=resolved_at)
            _same(actual, expected)
            _audit_append(store, audits, response, actor, candidate_id, start, finish)


def test_kg_reject_missing_foreign_and_merged_are_404_without_selected_effects(kg_reject_case):
    client, store, headers, pairs, merged = kg_reject_case
    missing = "er_0000000000000000"
    assert missing not in {row["id"] for row in _state(store)["entity_resolution_candidates"]}
    for candidate_id in (missing, pairs[FOREIGN]["candidate"], merged["candidate"]):
        before, audits = _state(store), _audits(store)
        response = client.post(f"/api/kg/resolutions/{candidate_id}/reject", headers=headers[MODERATOR])
        assert response.status_code == 404, response.text
        _same(response.json(), {"detail": "Resolution candidate not found"})
        _same(_state(store), before)
        _same(_audits(store), audits)


def test_kg_reject_user_and_guest_are_403_without_selected_effects(kg_reject_case):
    client, store, headers, pairs, _ = kg_reject_case
    for actor in (USER, GUEST):
        candidate_id = pairs[actor]["candidate"]
        before, audits = _state(store), _audits(store)
        response = client.post(f"/api/kg/resolutions/{candidate_id}/reject", headers=headers[actor])
        assert response.status_code == 403, response.text
        _same(response.json(), {"detail": "Access denied for kg.merge (default_deny)"})
        _same(_state(store), before)
        _same(_audits(store), audits)
