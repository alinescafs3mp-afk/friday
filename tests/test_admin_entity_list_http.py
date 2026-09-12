"""Exact owner HTTP contract for the bounded admin entity listing."""

from __future__ import annotations

import json
import re
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from friday.permissions import LEGACY_OWNER_USER_ID
from friday.server import create_app
from friday.storage.models import Entity, new_id

P = "entity-list-person-p"
Q = "entity-list-person-q"
STAMP = "2024-01-01T00:00:00+00:00"


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _id(value, prefix):
    assert type(value) is str and re.fullmatch(prefix + r"_[0-9a-f]{16}", value)
    return value


def _time(value, before, after):
    assert type(value) is str
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    assert parsed.tzinfo is not None and parsed.utcoffset() == timedelta(0)
    assert before.replace(microsecond=0) <= parsed <= after
    return value


def _state(store):
    return {
        table: [
            dict(row)
            for row in store.execute(
                f"SELECT * FROM {table} WHERE user_id IN (?, ?) ORDER BY rowid", (P, Q)
            ).fetchall()
        ]
        for table in ("entities", "entity_versions")
    }


def _audits(store):
    return [dict(row) for row in store.execute("SELECT * FROM audit_log ORDER BY rowid").fetchall()]


def _card(entity_id, name, entity_type, aliases, description):
    return {
        "id": entity_id,
        "name": name,
        "entity_type": entity_type,
        "aliases": aliases,
        "aliases_json": json.dumps(aliases, ensure_ascii=False),
        "description": description,
        "version": 1,
        "created_at": STAMP,
        "updated_at": STAMP,
    }


def _report(items, *, total, limit, offset):
    return {
        "user_id": P,
        "items": items,
        "count": len(items),
        "total": total,
        "matched_at_least": total,
        "truncated": offset + len(items) < total,
        "limit": limit,
        "offset": offset,
    }


def _audit_append(store, previous, response, start, finish):
    rows = _audits(store)
    assert len(rows) == len(previous) + 1
    assert rows[:-1] == previous
    row = rows[-1]
    audit_id = _id(row["id"], "audit")
    assert audit_id not in {item["id"] for item in previous}
    request_id = response.headers["x-request-id"]
    assert type(request_id) is str and re.fullmatch(r"[0-9a-f]{24}", request_id)
    assert row == {
        "id": audit_id,
        "user_id": LEGACY_OWNER_USER_ID,
        "action": "admin.entities.read",
        "target_type": "user",
        "target_id": P,
        "before_json": None,
        "after_json": None,
        "ip_address": "",
        "request_id": request_id,
        "created_at": _time(row["created_at"], start, finish),
    }
    return rows


@pytest.fixture
def entity_list_admin(settings):
    with TestClient(create_app(replace(settings, shared_archive=False))) as client:
        store = client.app.state.storage
        for person in (P, Q):
            store.ensure_user(person, preset_key="user")
        definitions = (
            (P, "Alpha Project", "project", ["Alpha Alias"], "First target project"),
            (P, "Beta Project", "project", ["Beta Alias"], "Second target project"),
            (P, "Gamma Person", "person", ["Gamma Alias"], "Target person"),
            (Q, "Alpha Project", "project", ["Foreign Alias"], "Foreign project"),
        )
        seeded = [
            Entity(
                id=new_id("ent"),
                user_id=person,
                name=name,
                entity_type=entity_type,
                aliases_json=aliases,
                description=description,
                metadata_json={"canary": f"{person}:{name}"},
                created_at=STAMP,
                updated_at=STAMP,
            )
            for person, name, entity_type, aliases, description in definitions
        ]
        seed_start = datetime.now(UTC)
        for entity in seeded:
            store.create_entity(entity)
        seed_finish = datetime.now(UTC)
        # Native generated IDs were retained through every managed storage write.
        entity_ids = [str(entity.id) for entity in seeded]
        assert all(type(entity_id) is str for entity_id in entity_ids)
        expected_rows = []
        cards = []
        for entity_id, definition in zip(entity_ids, definitions, strict=True):
            person, name, entity_type, aliases, description = definition
            _id(entity_id, "ent")
            expected_rows.append(
                {
                    "id": entity_id,
                    "user_id": person,
                    "name": name,
                    "normalized_name": name.casefold(),
                    "entity_type": entity_type,
                    "aliases_json": _json(aliases),
                    "description": description,
                    "metadata_json": _json({"canary": f"{person}:{name}"}),
                    "canonical": 1,
                    "merged_into_id": None,
                    "version": 1,
                    "created_at": STAMP,
                    "updated_at": STAMP,
                    "deleted_at": None,
                }
            )
            cards.append(_card(entity_id, name, entity_type, aliases, description))
        baseline = _state(store)
        assert baseline["entities"] == expected_rows
        assert len(baseline["entity_versions"]) == len(expected_rows)
        for version, expected in zip(baseline["entity_versions"], expected_rows, strict=True):
            assert version == {
                "id": _id(version["id"], "entv"),
                "user_id": expected["user_id"],
                "entity_id": expected["id"],
                "version": 1,
                "snapshot_json": _json(expected),
                "created_at": _time(version["created_at"], seed_start, seed_finish),
            }
        owner = {"Authorization": f"Bearer {settings.api_token}"}
        yield client, store, owner, baseline, cards


def test_admin_entities_lists_filtered_paginated_target_without_foreign_rows(entity_list_admin):
    client, store, owner, baseline, cards = entity_list_admin
    alpha, beta, gamma, _foreign = cards
    audit_prefix = _audits(store)

    start = datetime.now(UTC)
    first = client.get(
        "/api/admin/entities",
        params={"user_id": P, "entity_type": "project", "limit": 1, "offset": 0},
        headers=owner,
    )
    finish = datetime.now(UTC)
    assert first.status_code == 200, first.text
    assert first.json() == _report([alpha], total=2, limit=1, offset=0)
    assert set(first.json()) == {
        "user_id",
        "items",
        "count",
        "total",
        "matched_at_least",
        "truncated",
        "limit",
        "offset",
    }
    assert set(first.json()["items"][0]) == {
        "id",
        "name",
        "entity_type",
        "aliases",
        "aliases_json",
        "description",
        "version",
        "created_at",
        "updated_at",
    }
    assert _state(store) == baseline
    audit_prefix = _audit_append(store, audit_prefix, first, start, finish)

    start = datetime.now(UTC)
    second = client.get(
        "/api/admin/entities",
        params={"user_id": P, "entity_type": "project", "limit": 5000, "offset": 1},
        headers=owner,
    )
    finish = datetime.now(UTC)
    assert second.status_code == 200, second.text
    assert second.json() == _report([beta], total=2, limit=200, offset=1)
    assert _state(store) == baseline
    audit_prefix = _audit_append(store, audit_prefix, second, start, finish)

    start = datetime.now(UTC)
    all_entities = client.get("/api/admin/entities", params={"user_id": P, "limit": 5000}, headers=owner)
    finish = datetime.now(UTC)
    assert all_entities.status_code == 200, all_entities.text
    assert all_entities.json() == _report([alpha, beta, gamma], total=3, limit=200, offset=0)
    assert _state(store) == baseline
    _audit_append(store, audit_prefix, all_entities, start, finish)


def test_admin_entities_invalid_filters_do_not_read_or_audit(entity_list_admin):
    client, store, owner, baseline, _ = entity_list_admin
    audit_prefix = _audits(store)

    invalid_type = client.get(
        "/api/admin/entities", params={"user_id": P, "entity_type": "thought"}, headers=owner
    )
    assert invalid_type.status_code == 400
    assert invalid_type.json() == {"detail": "Недопустимый тип сущности"}
    assert _state(store) == baseline and _audits(store) == audit_prefix

    cases = (
        (
            {"user_id": P, "limit": 0},
            {
                "type": "greater_than_equal",
                "loc": ["query", "limit"],
                "msg": "Input should be greater than or equal to 1",
                "input": "0",
                "ctx": {"ge": 1},
            },
        ),
        (
            {"user_id": P, "limit": 5001},
            {
                "type": "less_than_equal",
                "loc": ["query", "limit"],
                "msg": "Input should be less than or equal to 5000",
                "input": "5001",
                "ctx": {"le": 5000},
            },
        ),
        (
            {"user_id": P, "offset": -1},
            {
                "type": "greater_than_equal",
                "loc": ["query", "offset"],
                "msg": "Input should be greater than or equal to 0",
                "input": "-1",
                "ctx": {"ge": 0},
            },
        ),
    )
    for params, detail in cases:
        refused = client.get("/api/admin/entities", params=params, headers=owner)
        assert refused.status_code == 422
        assert refused.json() == {"detail": [detail]}
        assert _state(store) == baseline and _audits(store) == audit_prefix
