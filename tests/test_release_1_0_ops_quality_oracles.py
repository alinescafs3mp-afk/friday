"""Quality HTTP: seeded totals, complete pages, authority, audit and no writes.

Model-free representative dashboard contract, not scale or concurrent ingestion.
Expected counters are literals; no production dashboard/count/list builds them.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import replace
from datetime import UTC, datetime

import httpx
import pytest
from fastapi.testclient import TestClient

from friday.knowledge_graph import KnowledgeGraph
from friday.permissions import LEGACY_OWNER_USER_ID
from friday.storage.models import EntityType
from tests.test_api_tokens import _issue
from tests.test_organs_profile_chronicle import _seed_knowledge
from tests.test_release_1_0_knowledge_read_oracles import _audits, _read_state
from tests.test_release_1_0_profile_oracles import _A, _B
from tests.test_release_1_0_profile_oracles import profile_http as profile_http

ROOT = "/api/admin/quality"
ADMIN = "local:r10-quality-admin"
STALE = "2001-01-01T00:00:00+00:00"
FOREIGN = "FOREIGN_QUALITY_PRIVATE_046"
MISSING = "local:r10-quality-missing"
pytestmark = pytest.mark.parametrize("profile_http", [False], indirect=True, ids=["personal"])


def _equal(actual, expected, code):
    assert actual == expected, (code, actual, expected)


def _state(ctx):
    result = _read_state(ctx)
    for table in ("knowledge_usage", "knowledge_conflicts", "feedback", "feedback_state"):
        result[table] = [
            dict(row) for row in ctx["storage"].execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
        ]
    return result


@pytest.fixture
def quality_http(profile_http):
    ctx, storage = profile_http, profile_http["storage"]
    for role, person, preset in (("owner", LEGACY_OWNER_USER_ID, "owner"), ("admin", ADMIN, "admin")):
        token = "jrc_synthetic_quality_" + role
        _issue(storage, person, preset, token)
        ctx[role] = {"Authorization": "Bearer " + token}
    storage.set_permission_override(_A, "admin.all_data.read", "allow")
    storage.set_permission_override(_B, "admin.all_data.read", "deny")
    ctx["ids"], ctx["seed"] = {}, {}
    for person, count in ((_A, 5), (_B, 2)):
        ids = [
            _seed_knowledge(storage, person, f"QUALITY_A_{n}" if person == _A else f"{FOREIGN}_{n}", [])
            for n in range(count)
        ]
        ctx["ids"][person] = ids
        graph = KnowledgeGraph(storage)
        entities = [
            graph.create_entity(person, f"quality-{person}-{n}", EntityType.PERSON)["id"] for n in range(2)
        ]
        with storage.transaction() as conn:
            for key in ids[:3]:
                conn.execute(
                    "UPDATE knowledge_objects SET importance=0.1,quality_score=0.1,promotion_score=0.1,"
                    "created_at=?,updated_at=? WHERE id=?",
                    (STALE, STALE, key),
                )
            conn.execute(
                "INSERT INTO inbox(id,user_id,raw_object_id,knowledge_object_id,created_at) "
                "SELECT ?,user_id,raw_object_id,id,? FROM knowledge_objects WHERE id=?",
                ("inbox-quality-" + person, STALE, ids[0]),
            )
            conn.execute(
                "INSERT INTO relation_candidates(id,user_id,source_entity_id,target_entity_id,created_at) "
                "VALUES(?,?,?,?,?)",
                ("candidate-quality-" + person, person, *entities, STALE),
            )
        storage.store_knowledge_conflict(person, ids[0], ids[1], confidence=0.8)
    a, b = ctx["ids"][_A], ctx["ids"][_B]
    with storage.transaction() as conn:
        for person, key, counts in (
            (_A, a[3], (7, 3, 2, 1)),
            (_A, a[4], (2, 1, 0, 4)),
            (_B, b[0], (99, 99, 99, 99)),
        ):
            conn.execute(
                "INSERT INTO knowledge_usage(user_id,knowledge_object_id,retrieval_count,answer_count,"
                "positive_feedback_count,negative_feedback_count,updated_at) VALUES(?,?,?,?,?,?,?)",
                (person, key, *counts, STALE),
            )
        for number, person, key, kind, score, current in (
            (0, _A, a[3], "classification", 1, False),
            (1, _A, a[3], "classification", -1, True),
            (2, _A, a[4], "general", 0.5, True),
            (3, _B, b[0], "classification", 1, True),
        ):
            fid = f"feedback-quality-{number}"
            conn.execute(
                "INSERT INTO feedback(id,user_id,target_type,target_id,feedback_type,score,created_at) "
                "VALUES(?,?,'knowledge_object',?,?,?,?)",
                (fid, person, key, kind, score, STALE),
            )
            if current:
                conn.execute(
                    "INSERT INTO feedback_state(user_id,target_type,target_id,feedback_type,score,feedback_id,updated_at) "
                    "VALUES(?,'knowledge_object',?,?,?,?,?)",
                    (person, key, kind, score, fid, STALE),
                )
    # Persisted input facts, captured before any HTTP observation. Derived fields
    # below are explicit, independent expectations for three identical stale KOs.
    for key in a[:3]:
        ctx["seed"][key] = dict(
            storage.execute("SELECT * FROM knowledge_objects WHERE id=?", (key,)).fetchone()
        )
    yield ctx


def _candidate(ctx, key):
    fields = (
        "id",
        "user_id",
        "title",
        "knowledge_kind",
        "content_type",
        "metadata_json",
        "importance",
        "quality_score",
        "promotion_score",
        "lifecycle_stage",
        "created_at",
        "updated_at",
        "summary",
        "content",
    )
    ko = {field: ctx["seed"][key][field] for field in fields}
    ko.update(
        dict.fromkeys(
            (
                "retrieval_count",
                "answer_count",
                "positive_feedback_count",
                "negative_feedback_count",
                "last_retrieved_at",
                "last_used_at",
                "last_feedback_at",
            )
        )
    )
    return {
        "knowledge_object": ko,
        "risk_score": 0.884,
        "recommended_action": "review_for_archive",
        "suggested_importance": 0.1,
        "reasons": [
            "not updated within threshold",
            "low importance",
            "low quality score",
            "weak original promotion confidence",
            "never used",
        ],
        "protected": False,
    }


def _expected(ctx, limit, offset):
    return {
        "user_id": _A,
        "graph": {
            "entity_count": 2,
            "relation_count": 0,
            "knowledge_object_count": 5,
            "raw_object_count": 5,
            "file_count": 0,
            "entities_by_type": {"person": 2},
            "pending_resolutions": 0,
            "pending_inbox": 1,
            "pending_relation_candidates": 1,
            "pending_conflicts": 1,
        },
        "usage": {"tracked": 2, "retrievals": 9, "answers": 4, "positive": 2, "negative": 5},
        "feedback": {
            "history": {
                "classification": {"avg_score": 0.0, "count": 2},
                "general": {"avg_score": 0.5, "count": 1},
            },
            "current_count": 2,
            "classification_current": 1,
            "classification_negative": 1,
        },
        "review_pressure": {
            "pending_inbox": 1,
            "relation_candidates": 1,
            "conflicts": 1,
            "lifecycle_candidates": 3,
        },
        "lifecycle_candidates": [
            _candidate(ctx, key) for key in sorted(ctx["ids"][_A][:3])[offset : offset + limit]
        ],
        "lifecycle_total": 3,
        "lifecycle_limit": limit,
        "lifecycle_offset": offset,
    }


def _privacy(ctx, response):
    encoded = response.text + json.dumps(_audits(ctx), ensure_ascii=False)
    assert FOREIGN not in encoded and "PRIVATE_STYLE_" not in encoded and "jrc_synthetic_" not in encoded, (
        "quality_privacy"
    )


def _page(ctx, role, *, limit=None, offset=None):
    before, history = _state(ctx), _audits(ctx)
    started = datetime.now(UTC).replace(microsecond=0)
    params = {"user_id": _A}
    if limit is not None:
        params["lifecycle_limit"] = limit
    if offset is not None:
        params["lifecycle_offset"] = offset
    response = ctx["client"].get(ROOT, headers=ctx[role], params=params)
    _equal(response.status_code, 200, "quality_status")
    _privacy(ctx, response)
    _equal(
        response.json(),
        _expected(ctx, 50 if limit is None else limit, 0 if offset is None else offset),
        "quality_exact_dashboard",
    )
    _equal(_state(ctx), before, "quality_read_state")
    rows = _audits(ctx)
    _equal(rows[: len(history)], history, "quality_audit_prefix")
    added = rows[len(history) :]
    _equal(len(added), 0 if role == _A else 1, "quality_read_audit_count")
    if added:
        row = added[0]
        _equal(
            (
                row["user_id"],
                row["action"],
                row["target_type"],
                row["target_id"],
                row["before_json"],
                row["after_json"],
            ),
            (
                ADMIN if role == "admin" else LEGACY_OWNER_USER_ID,
                "admin.quality.read",
                "user",
                _A,
                None,
                None,
            ),
            "quality_read_audit_provenance",
        )
        assert row["id"] and row["id"] not in {old["id"] for old in history}, "quality_audit_identity"
        assert started <= datetime.fromisoformat(row["created_at"]) <= datetime.now(UTC), (
            "quality_audit_clock"
        )


def _walk(ctx):
    _page(ctx, "owner")
    for offset in (0, 2, 4):
        _page(ctx, "admin", limit=2, offset=offset)
    _page(ctx, _A, limit=1, offset=1)


def test_quality_dashboard_binds_literal_totals_complete_pages_and_cross_person_audit(quality_http):
    _walk(quality_http)


def _refused(ctx):
    ctx["storage"].set_permission_override(ADMIN, "admin.all_data.read", "deny")
    # The requested person's content is private on refusal. Capture fixture
    # preimages before HTTP, never from the possibly corrupt response body.
    requested_canaries = tuple(
        value
        for row in ctx["storage"]
        .execute("SELECT title,content FROM knowledge_objects WHERE user_id=?", (_A,))
        .fetchall()
        for value in row
        if value
    )
    assert len(requested_canaries) == 10, "quality_refusal_fixture"
    for role, params, status in (
        (None, {"user_id": _A}, 401),
        (_B, {"user_id": _A}, 403),
        ("admin", {"user_id": _A}, 403),
        ("owner", {}, 422),
        ("owner", {"user_id": _A, "lifecycle_limit": 0}, 422),
        ("owner", {"user_id": _A, "lifecycle_limit": 501}, 422),
        ("owner", {"user_id": _A, "lifecycle_offset": -1}, 422),
        ("owner", {"user_id": _A, "lifecycle_limit": "bad"}, 422),
        ("owner", {"user_id": MISSING}, 404),
    ):
        before, history = _state(ctx), _audits(ctx)
        started = datetime.now(UTC).replace(microsecond=0)
        response = ctx["client"].get(ROOT, headers=ctx[role] if role else {}, params=params)
        _equal(response.status_code, status, "quality_refusal_status")
        _privacy(ctx, response)
        assert all(value not in response.text for value in requested_canaries), (
            "quality_refusal_requested_privacy"
        )
        assert "lifecycle_candidates" not in response.json(), "quality_refusal_no_dashboard"
        assert all(key not in response.text for ids in ctx["ids"].values() for key in ids), (
            "quality_refusal_no_rows"
        )
        _equal(_state(ctx), before, "quality_refusal_state")
        after = _audits(ctx)
        _equal(after[: len(history)], history, "quality_audit_prefix")
        # The existing handler logs the requested foreign read before its 404.
        # Auth and query refusals never reach that read operation.
        if status != 404:
            assert not any(row["action"] == "admin.quality.read" for row in after[len(history) :]), (
                "quality_refusal_no_read"
            )
        else:
            _equal(response.json(), {"detail": "Пользователь не найден"}, "quality_missing_body")
            added = after[len(history) :]
            _equal(len(added), 1, "quality_missing_audit_count")
            row = added[0]
            # Unknown account targets are HMAC references in durable audit rows.
            # Independently bind that reference to the exact requested account.
            key = bytes.fromhex(
                ctx["storage"]
                .execute("SELECT value FROM schema_meta WHERE key='audit_privacy_hmac_key'")
                .fetchone()[0]
            )
            target = (
                "user:ref:"
                + hmac.new(key, ("target:user\0" + MISSING).encode(), hashlib.sha256).hexdigest()[:24]
            )
            _equal(
                (
                    row["user_id"],
                    row["action"],
                    row["target_type"],
                    row["target_id"],
                    row["before_json"],
                    row["after_json"],
                ),
                (LEGACY_OWNER_USER_ID, "admin.quality.read", "user", target, None, None),
                "quality_missing_audit_provenance",
            )
            assert row["id"] and row["id"] not in {old["id"] for old in history}, (
                "quality_missing_audit_identity"
            )
            assert started <= datetime.fromisoformat(row["created_at"]) <= datetime.now(UTC), (
                "quality_missing_audit_clock"
            )


def test_quality_refuses_missing_invalid_denied_revoked_and_anonymous_without_writes(quality_http):
    _refused(quality_http)


_FAULTS = (
    ("wrong_usage", "quality_exact_dashboard", "walk"),
    ("wrong_feedback", "quality_exact_dashboard", "walk"),
    ("wrong_graph", "quality_exact_dashboard", "walk"),
    ("pressure_is_page_length", "quality_exact_dashboard", "walk"),
    ("ignore_offset", "quality_exact_dashboard", "walk"),
    ("missing_candidate", "quality_exact_dashboard", "walk"),
    ("wrong_candidate_risk", "quality_exact_dashboard", "walk"),
    ("missing_audit", "quality_read_audit_count", "walk"),
    ("wrong_audit_actor", "quality_read_audit_provenance", "walk"),
    ("wrong_audit_target", "quality_read_audit_provenance", "walk"),
    ("read_own_write", "quality_read_state", "walk"),
    ("read_foreign_write", "quality_read_state", "walk"),
    ("refusal_permission_write", "quality_refusal_state", "refused"),
    ("refusal_secret", "quality_privacy", "refused"),
    ("requested_reflection_401", "quality_refusal_requested_privacy", "refused"),
    ("requested_reflection_403", "quality_refusal_requested_privacy", "refused"),
    ("requested_reflection_404", "quality_refusal_requested_privacy", "refused"),
    ("requested_reflection_422", "quality_refusal_requested_privacy", "refused"),
    ("missing_person_audit", "quality_missing_audit_count", "refused"),
    ("missing_person_actor", "quality_missing_audit_provenance", "refused"),
    ("missing_person_target", "quality_missing_audit_provenance", "refused"),
)


@pytest.mark.parametrize("fault,code,scenario", _FAULTS, ids=[row[0] for row in _FAULTS])
def test_quality_oracles_detect_actual_http_audit_and_persisted_faults(
    quality_http, monkeypatch, fault, code, scenario
):
    ctx, injected = quality_http, []
    storage, real_request = ctx["storage"], TestClient.request
    real_audit = storage.log_audit
    if fault in {"missing_audit", "wrong_audit_actor", "wrong_audit_target"}:

        def alter_audit(entry):
            if entry.action == "admin.quality.read":
                injected.append(True)
                if fault == "missing_audit":
                    return None
                entry = replace(
                    entry, **({"user_id": _B} if fault == "wrong_audit_actor" else {"target_id": _B})
                )
            return real_audit(entry)

        monkeypatch.setattr(storage, "log_audit", alter_audit)

    if fault in {"missing_person_audit", "missing_person_actor", "missing_person_target"}:

        def alter_missing_audit(entry):
            if entry.action == "admin.quality.read" and entry.target_id == MISSING:
                injected.append(True)
                if fault == "missing_person_audit":
                    return None
                entry = replace(
                    entry, **({"user_id": _B} if fault == "missing_person_actor" else {"target_id": _B})
                )
            return real_audit(entry)

        monkeypatch.setattr(storage, "log_audit", alter_missing_audit)

    def alter_request(self, method, url, **kwargs):
        response = real_request(self, method, url, **kwargs)
        if injected or method != "GET" or url != ROOT:
            return response
        if fault.startswith("requested_reflection_") and response.status_code == int(fault.rsplit("_", 1)[1]):
            secret = storage.execute(
                "SELECT content FROM knowledge_objects WHERE id=?", (ctx["ids"][_A][0],)
            ).fetchone()[0]
            assert secret == "QUALITY_A_0", "quality_reflection_fixture"
            body = response.json()
            body["detail"] = secret
            injected.append(True)
            return httpx.Response(response.status_code, json=body, request=response.request)
        if response.status_code == 403 and fault == "refusal_permission_write":
            storage.set_permission_override(_B, "admin.all_data.read", "allow")
            row = storage.execute(
                "SELECT effect FROM user_permission_overrides WHERE user_id=? AND security_id=?",
                (_B, "admin.all_data.read"),
            ).fetchone()
            assert row and row[0] == "allow", "quality_fault_write_not_applied"
            injected.append(True)
            return response
        if response.status_code == 401 and fault == "refusal_secret":
            injected.append(True)
            return httpx.Response(401, json={"detail": FOREIGN}, request=response.request)
        if response.status_code != 200:
            return response
        body, params = response.json(), kwargs.get("params", {})
        if fault == "wrong_usage":
            body["usage"]["retrievals"] += 99
        elif fault == "wrong_feedback":
            body["feedback"]["current_count"] = 3
        elif fault == "wrong_graph":
            body["graph"]["entity_count"] += 2
        elif fault == "pressure_is_page_length" and params.get("lifecycle_limit") == 2:
            body["review_pressure"]["lifecycle_candidates"] = len(body["lifecycle_candidates"])
        elif fault == "ignore_offset" and params.get("lifecycle_offset") == 2:
            body["lifecycle_candidates"] = ctx["first_actual_page"]
        elif fault == "missing_candidate":
            body["lifecycle_candidates"].pop()
        elif fault == "wrong_candidate_risk":
            body["lifecycle_candidates"][0]["risk_score"] = 0.01
        elif fault in {"read_own_write", "read_foreign_write"}:
            key = ctx["ids"][_A if fault == "read_own_write" else _B][0]
            with storage.transaction() as conn:
                conn.execute(
                    "UPDATE knowledge_objects SET content='CORRUPTED_QUALITY_READ' WHERE id=?", (key,)
                )
            assert (
                storage.execute("SELECT content FROM knowledge_objects WHERE id=?", (key,)).fetchone()[0]
                == "CORRUPTED_QUALITY_READ"
            ), "quality_fault_write_not_applied"
        else:
            if fault == "ignore_offset" and params.get("lifecycle_offset") == 0:
                ctx["first_actual_page"] = body["lifecycle_candidates"]
            return response
        injected.append(True)
        return httpx.Response(200, json=body, request=response.request)

    monkeypatch.setattr(TestClient, "request", alter_request)
    with pytest.raises(AssertionError, match=code):
        {"walk": _walk, "refused": _refused}[scenario](ctx)
    assert injected, "quality_fault_not_injected"
