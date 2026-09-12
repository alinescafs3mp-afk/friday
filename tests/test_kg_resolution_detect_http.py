"""Real own-tenant detection with a fixed positive pair and identifier negative control.
Private unexecuted harness draft; no product acceptance. Native IDs/timestamps are
bound from validated storage rows, while payload structure and values are literal.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from friday.server import create_app
from friday.storage.models import Entity, EntityResolutionCandidate, EntityType, new_id

_DETECT_P = "detect-person-p"
_DETECT_Q = "detect-person-q"
_DETECT_TABLES = (
    "entities",
    "entity_versions",
    "raw_objects",
    "knowledge_objects",
    "knowledge_entity_links",
    "relations",
    "entity_merge_history",
)


def _detect_same(actual, expected):
    assert type(actual) is type(expected)
    if isinstance(expected, dict):
        assert actual.keys() == expected.keys()
        for key in expected:
            _detect_same(actual[key], expected[key])
    elif isinstance(expected, list):
        assert len(actual) == len(expected)
        for left, right in zip(actual, expected, strict=True):
            _detect_same(left, right)
    else:
        assert actual == expected


def _detect_time(value, start, finish):
    assert isinstance(value, str)
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    assert parsed.tzinfo is not None and parsed.utcoffset() == timedelta(0)
    assert start.replace(microsecond=0) <= parsed <= finish
    return value


def _detect_rows(store, table):
    return [
        dict(row)
        for row in store.execute(
            f"SELECT * FROM {table} WHERE user_id IN (?, ?) ORDER BY rowid", (_DETECT_P, _DETECT_Q)
        ).fetchall()
    ]


def _detect_http_case(settings, names, aliases, public, evidence):
    with TestClient(
        create_app(replace(settings, shared_archive=False, api_require_token_on_loopback=True)),
        raise_server_exceptions=False,
    ) as client:
        store = client.app.state.storage
        native = {}
        for person in (_DETECT_P, _DETECT_Q):
            store.ensure_user(person, preset_key="user")
            native[person] = (new_id("ent"), new_id("ent"))
            for entity_id, name in zip(native[person], names, strict=True):
                store.create_entity(
                    Entity(
                        id=entity_id,
                        user_id=person,
                        name=name,
                        entity_type=EntityType.CONCEPT,
                        aliases_json=list(aliases),
                        metadata_json={"fixture": "DETECT_PRIVATE_CANARY"},
                        created_at="2024-01-01T00:00:00+00:00",
                        updated_at="2024-01-01T00:00:00+00:00",
                    )
                )
        store.store_resolution_candidate(
            EntityResolutionCandidate(
                id=new_id("er"),
                user_id=_DETECT_Q,
                entity_a_id=native[_DETECT_Q][0],
                entity_b_id=native[_DETECT_Q][1],
                confidence=0.995,
                resolution_method="exact_name_or_alias",
                evidence_json={"fixture": "FOREIGN_PRIVATE_CANARY"},
                created_at="2024-01-01T00:00:00+00:00",
            )
        )
        token = "kg-detect-synthetic-token"
        store.create_api_token(_DETECT_P, hashlib.sha256(token.encode()).hexdigest(), created_by=_DETECT_P)
        own_key = "entity_dedup:cursor:" + _DETECT_P
        foreign_key = "entity_dedup:cursor:" + _DETECT_Q
        store.kv_set(foreign_key, '{"after_key": null, "sweeps": 7}')
        left, right = (str(value) for value in native[_DETECT_P])
        before = {table: _detect_rows(store, table) for table in _DETECT_TABLES}
        candidates_before = _detect_rows(store, "entity_resolution_candidates")
        assert len(candidates_before) == 1
        assert candidates_before[0]["user_id"] == _DETECT_Q
        runtime_before = [
            dict(row)
            for row in store.execute(
                "SELECT * FROM runtime_kv WHERE key IN (?, ?) ORDER BY key", (own_key, foreign_key)
            ).fetchall()
        ]
        assert len(runtime_before) == 1 and runtime_before[0]["key"] == foreign_key
        audits_before = [
            dict(row) for row in store.execute("SELECT * FROM audit_log ORDER BY rowid").fetchall()
        ]
        start = datetime.now(UTC)
        response = client.post("/api/kg/resolutions/detect", headers={"Authorization": f"Bearer {token}"})
        finish = datetime.now(UTC)
        assert response.status_code == 200, response.text
        _detect_same({table: _detect_rows(store, table) for table in _DETECT_TABLES}, before)
        candidates_after = _detect_rows(store, "entity_resolution_candidates")
        expected_candidates = list(candidates_before)
        if evidence is not None:
            assert len(candidates_after) == 2
            candidate = candidates_after[-1]
            candidate_id = candidate["id"]
            assert isinstance(candidate_id, str)
            assert re.fullmatch("er_[0-9a-f]{16}", candidate_id)
            assert candidate_id != candidates_before[0]["id"]
            expected_candidates.append(
                {
                    "id": candidate_id,
                    "user_id": _DETECT_P,
                    "entity_a_id": left,
                    "entity_b_id": right,
                    "pair_key": "|".join(sorted((left, right))),
                    "confidence": 0.995,
                    "resolution_method": "exact_name_or_alias",
                    "evidence_json": json.dumps(evidence, ensure_ascii=False, sort_keys=True),
                    "status": "suggested",
                    "resolved_by": None,
                    "created_at": _detect_time(candidate["created_at"], start, finish),
                    "resolved_at": None,
                }
            )
        _detect_same(candidates_after, expected_candidates)
        runtime_after = [
            dict(row)
            for row in store.execute(
                "SELECT * FROM runtime_kv WHERE key IN (?, ?) ORDER BY key", (own_key, foreign_key)
            ).fetchall()
        ]
        assert len(runtime_after) == 2
        _detect_same(
            runtime_after,
            [
                {
                    "key": own_key,
                    "value": '{"after_key": null, "sweeps": 1}',
                    "updated_at": _detect_time(runtime_after[0]["updated_at"], start, finish),
                },
                *runtime_before,
            ],
        )
        audits_after = [
            dict(row) for row in store.execute("SELECT * FROM audit_log ORDER BY rowid").fetchall()
        ]
        _detect_same(audits_after, audits_before)
        items = []
        if evidence is not None:
            created = expected_candidates[-1]
            items = [
                {
                    "id": created["id"],
                    "entity_a_id": left,
                    "entity_b_id": right,
                    "confidence": 0.995,
                    "resolution_method": "exact_name_or_alias",
                    "status": "suggested",
                    "created_at": created["created_at"],
                    "resolved_at": "",
                    "entity_a": {
                        "id": left,
                        "name": names[0],
                        "entity_type": "concept",
                        "knowledge_count": 0,
                        "relation_count": 0,
                    },
                    "entity_b": {
                        "id": right,
                        "name": names[1],
                        "entity_type": "concept",
                        "knowledge_count": 0,
                        "relation_count": 0,
                    },
                    "recommendation": "strong_merge_candidate",
                }
            ]
        _detect_same(
            response.json(),
            {
                "items": items,
                "count": len(items),
                "total": len(items),
                "matched_at_least": len(items),
                "truncated": False,
                "scan": public,
            },
        )
        assert "PRIVATE_CANARY" not in response.text


def test_kg_resolution_detect_shared_alias_has_one_exact_candidate(settings):
    _detect_http_case(
        settings,
        ("alpha", "omega"),
        ("shared",),
        {
            "entities": 2,
            "pairs_examined": 1,
            "keys_total": 15,
            "keys_examined": 15,
            "keys_pending": 0,
            "candidates": 1,
            "suggested": 1,
            "pending_total": 1,
            "sweeps": 1,
            "partial": False,
            "resumed": False,
            "complete": True,
        },
        {
            "left_name": "alpha",
            "right_name": "omega",
            "name_similarity": 0.2,
            "sorted_token_similarity": 0.2,
            "token_jaccard": 0.0,
            "alias_similarity": 1.0,
            "exact_alias": True,
            "acronym_match": False,
            "shared_knowledge": 0.0,
            "shared_graph_neighbours": 0.0,
            "knowledge_object_ids": [],
            "graph_neighbour_entity_ids": [],
            "identifier_safe": True,
        },
    )


def test_kg_resolution_detect_compact_identifiers_have_no_candidate(settings):
    _detect_http_case(
        settings,
        ("BRK.A", "BRK.B"),
        (),
        {
            "entities": 2,
            "pairs_examined": 1,
            "keys_total": 10,
            "keys_examined": 10,
            "keys_pending": 0,
            "candidates": 0,
            "suggested": 0,
            "pending_total": 0,
            "sweeps": 1,
            "partial": False,
            "resumed": False,
            "complete": True,
        },
        None,
    )
