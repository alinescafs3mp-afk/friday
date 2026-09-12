"""Admin entity writes preserve tenant rows, version history and observable audit."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from friday.permissions import LEGACY_OWNER_USER_ID
from friday.server import create_app
from friday.storage.models import Entity, new_id

P = "entity-write-person-p"
Q = "entity-write-person-q"
STAMP = "2024-01-01T00:00:00+00:00"


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _rows(store, table):
    sql = {
        "entities": "SELECT * FROM entities ORDER BY rowid",
        "entity_versions": "SELECT * FROM entity_versions ORDER BY rowid",
        "audit_log": "SELECT * FROM audit_log ORDER BY rowid",
    }
    return [dict(row) for row in store.execute(sql[table]).fetchall()]


def _state(store):
    return {table: _rows(store, table) for table in ("entities", "entity_versions", "audit_log")}


def _id(value, prefix):
    assert isinstance(value, str) and re.fullmatch(prefix + r"_[0-9a-f]{16}", value)
    return value


def _time(value, before, after):
    assert isinstance(value, str)
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    assert parsed.tzinfo is not None and parsed.utcoffset() == timedelta(0)
    assert before.replace(microsecond=0) <= parsed <= after
    return value


def _card(expected):
    # Input is a closed fixture-owned expected row, never a product observation.
    return {
        "id": expected["id"],
        "name": expected["name"],
        "entity_type": expected["entity_type"],
        "aliases": json.loads(expected["aliases_json"]),
        "aliases_json": expected["aliases_json"],
        "description": expected["description"],
        "version": expected["version"],
        "created_at": expected["created_at"],
        "updated_at": expected["updated_at"],
    }


def _fingerprint(expected):
    # Durable audit exposes counters; the five private raw fingerprint fields
    # are represented by their count after the audit privacy boundary.
    return {
        "id": expected["id"],
        "entity_type": expected["entity_type"],
        "version": expected["version"],
        "created_at": expected["created_at"],
        "updated_at": expected["updated_at"],
        "name_chars": len(expected["name"]),
        "description_chars": len(expected["description"]),
        "aliases_chars": len(expected["aliases_json"]),
        "private_fields_count": 5,
    }


def _version_append(store, previous, expected, start, finish):
    rows = _rows(store, "entity_versions")
    assert len(rows) == len(previous) + 1 and rows[:-1] == previous
    new = rows[-1]
    version_id = _id(new["id"], "entv")
    assert version_id not in {row["id"] for row in previous}
    recorded_at = _time(new["created_at"], start, finish)
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z", recorded_at)
    parsed = datetime.fromisoformat(recorded_at.replace("Z", "+00:00"))
    for prior in previous:
        if prior["entity_id"] == expected["id"]:
            assert parsed > datetime.fromisoformat(prior["created_at"].replace("Z", "+00:00"))
    assert new == {
        "id": version_id,
        "user_id": expected["user_id"],
        "entity_id": expected["id"],
        "version": expected["version"],
        "snapshot_json": _json(expected),
        "created_at": recorded_at,
    }


def _audit_append(store, previous, response, action, entity_id, start, finish, *, before=None, after=None):
    rows = _rows(store, "audit_log")
    assert len(rows) == len(previous) + 1 and rows[:-1] == previous
    row = rows[-1]
    audit_id = _id(row["id"], "audit")
    assert audit_id not in {old["id"] for old in previous}
    created = _time(row["created_at"], start, finish)
    request_id = response.headers["x-request-id"]
    assert re.fullmatch(r"[0-9a-f]{24}", request_id)
    assert row == {
        "id": audit_id,
        "user_id": LEGACY_OWNER_USER_ID,
        "action": action,
        "target_type": "entity",
        "target_id": entity_id,
        "before_json": _json(before) if before is not None else None,
        "after_json": _json(after) if after is not None else None,
        "ip_address": "",
        "request_id": request_id,
        "created_at": created,
    }


@pytest.fixture
def entity_admin(settings):
    with TestClient(create_app(replace(settings, shared_archive=False))) as client:
        store = client.app.state.storage
        for person in (P, Q):
            store.ensure_user(person, preset_key="user")
        secret = "entity-mutation-user-secret-" + "P" * 32
        store.create_api_token(
            P, hashlib.sha256(secret.encode()).hexdigest(), label="entity-write", created_by="test"
        )
        ids = [_id(new_id("ent"), "ent"), _id(new_id("ent"), "ent")]
        for entity_id, person, name in zip(ids, (P, Q), ("Old Project", "Foreign Project"), strict=True):
            store.create_entity(
                Entity(
                    id=entity_id,
                    user_id=person,
                    name=name,
                    entity_type="project",
                    aliases_json=["Old"],
                    description="Before",
                    metadata_json={"canary": "retain"},
                    created_at=STAMP,
                    updated_at=STAMP,
                )
            )
        owner = {"Authorization": f"Bearer {settings.api_token}"}
        denied = {"Authorization": f"Bearer {secret}"}
        yield client, store, owner, denied, ids[0]


def test_admin_entity_create_persists_exact_target_row_and_first_version(entity_admin):
    client, store, owner, denied, _ = entity_admin
    before = _state(store)
    payload = {
        "user_id": P,
        "name": "  Alpha   Plan ",
        "entity_type": "project",
        "aliases": ["z", " A ", "z"],
        "description": "Created",
        "metadata": {"spoof": "ignored"},
        "version": 77,
    }
    refused = client.post("/api/admin/entities", json=payload, headers=denied)
    assert refused.status_code == 403
    assert refused.json() == {"detail": "Access denied for admin.all_data.manage (default_deny)"}
    assert _state(store) == before
    start = datetime.now(UTC)
    response = client.post("/api/admin/entities", json=payload, headers=owner)
    finish = datetime.now(UTC)
    assert response.status_code == 200, response.text
    item = response.json()["entity"]
    entity_id = _id(item["id"], "ent")
    assert entity_id not in {row["id"] for row in before["entities"]}
    expected = {
        "id": entity_id,
        "user_id": P,
        "name": "Alpha Plan",
        "normalized_name": "alpha plan",
        "entity_type": "project",
        "aliases_json": '["A", "z"]',
        "description": "Created",
        "metadata_json": "{}",
        "canonical": 1,
        "merged_into_id": None,
        "version": 1,
        "created_at": _time(item["created_at"], start, finish),
        "updated_at": _time(item["updated_at"], start, finish),
        "deleted_at": None,
    }
    assert response.json() == {"entity": _card(expected)}
    assert _rows(store, "entities") == [*before["entities"], expected]
    _version_append(store, before["entity_versions"], expected, start, finish)
    _audit_append(
        store,
        before["audit_log"],
        response,
        "admin.entity.create",
        entity_id,
        start,
        finish,
        after=_fingerprint(expected),
    )


def test_admin_entity_patch_preserves_foreign_rows_and_appends_exact_revision(entity_admin):
    client, store, owner, _, entity_id = entity_admin
    before = _state(store)
    old = next(row for row in before["entities"] if row["id"] == entity_id)
    payload = {
        "user_id": Q,
        "name": "  Beta   Plan ",
        "entity_type": "organization",
        "aliases": [" D ", "B", "B"],
        "description": "Updated",
        "metadata": {"spoof": "ignored"},
        "version": 77,
    }
    refused = client.patch(f"/api/admin/entities/{entity_id}", json=payload, headers=owner)
    assert refused.status_code == 404 and refused.json() == {"detail": "Сущность не найдена"}
    assert _state(store) == before
    payload["user_id"] = P
    start = datetime.now(UTC)
    response = client.patch(f"/api/admin/entities/{entity_id}", json=payload, headers=owner)
    finish = datetime.now(UTC)
    assert response.status_code == 200, response.text
    updated_at = _time(response.json()["entity"]["updated_at"], start, finish)
    expected = {
        **old,
        "name": "Beta   Plan",
        "normalized_name": "beta plan",
        "entity_type": "organization",
        "aliases_json": '["B", "D"]',
        "description": "Updated",
        "version": 2,
        "updated_at": updated_at,
    }
    assert response.json() == {"entity": _card(expected)}
    assert _rows(store, "entities") == [
        expected if row["id"] == entity_id else row for row in before["entities"]
    ]
    _version_append(store, before["entity_versions"], expected, start, finish)
    fingerprint = {
        **_fingerprint(expected),
        "changed_fields": ["aliases", "description", "entity_type", "name"],
    }
    # Organization is outside the audit enum vocabulary; its length is retained.
    del fingerprint["entity_type"]
    fingerprint["entity_type_chars"] = 12
    _audit_append(
        store,
        before["audit_log"],
        response,
        "admin.entity.update",
        entity_id,
        start,
        finish,
        before=_fingerprint(old),
        after=fingerprint,
    )


def test_admin_entity_delete_records_tombstone_once_and_preserves_other_tenant(entity_admin):
    client, store, owner, _, entity_id = entity_admin
    before = _state(store)
    old = next(row for row in before["entities"] if row["id"] == entity_id)
    start = datetime.now(UTC)
    response = client.delete(f"/api/admin/entities/{entity_id}", params={"user_id": P}, headers=owner)
    finish = datetime.now(UTC)
    assert response.status_code == 200 and response.json() == {"status": "soft_deleted"}
    rows = _rows(store, "entities")
    observed = next(row for row in rows if row["id"] == entity_id)
    expected = {
        **old,
        "canonical": 0,
        "version": 2,
        "updated_at": _time(observed["updated_at"], start, finish),
        "deleted_at": _time(observed["deleted_at"], start, finish),
    }
    assert rows == [expected if row["id"] == entity_id else row for row in before["entities"]]
    _version_append(store, before["entity_versions"], expected, start, finish)
    _audit_append(
        store,
        before["audit_log"],
        response,
        "admin.entity.delete",
        entity_id,
        start,
        finish,
        before=_fingerprint(old),
    )
    terminal = _state(store)
    replay = client.delete(f"/api/admin/entities/{entity_id}", params={"user_id": P}, headers=owner)
    assert replay.status_code == 404 and replay.json() == {"detail": "Сущность не найдена"}
    assert _state(store) == terminal
