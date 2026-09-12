"""Actual eval HTTP/auth/storage/search, with scripted vectors only in the chunk arm."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from friday.permissions import LEGACY_OWNER_USER_ID
from friday.server import create_app
from friday.workers import WorkersManager
from tests.test_api_tokens import _issue
from tests.test_chunk_recall import (
    QUERY,
    _embeddings_settings,
    _FakeTopicEmbeddings,
    _long_import,
    _make_ko,
)
from tests.test_eval_harness import _store

OTHER = "eval089-unrelated"
TARGET = "eval089-target"
PATHS = ("ablation", "chunk-ab")


@pytest.fixture
def eval_http(settings):
    current = replace(settings, shared_archive=False, eval_k=10, rerank_base_url="")
    assert not current.workers_enabled and not current.llm_enabled and not current.embeddings_enabled
    with TestClient(create_app(current), raise_server_exceptions=False) as client:
        store = client.app.state.storage
        headers = {"owner": {"Authorization": f"Bearer {current.api_token}"}, "anonymous": {}}
        for role in ("admin", "user"):
            token = f"jrc_eval089_{role}_synthetic_secret"
            _issue(store, f"eval089-{role}", role, token)
            headers[role] = {"Authorization": f"Bearer {token}"}
        for person in (LEGACY_OWNER_USER_ID, TARGET, OTHER):
            store.ensure_user(person)
        store.kv_set("eval:last_run:" + OTHER, '{"foreign_baseline":true}')
        yield client, store, current, headers


def _snapshot(store):
    return {
        name: [dict(r) for r in store.execute(f"SELECT * FROM {name} ORDER BY rowid").fetchall()]
        for name in ("raw_objects", "knowledge_objects", "eval_cases")
    }


def _audits(store, path):
    action = "admin.eval." + path.replace("-", "_")
    return [
        dict(r)
        for r in store.execute("SELECT * FROM audit_log WHERE action=? ORDER BY rowid", (action,)).fetchall()
    ]


def _audit_delta(store, path, before, response, actor, target):
    rows = _audits(store, path)
    assert rows[:-1] == before and len(rows) == len(before) + 1
    row = rows[-1]
    assert (row["user_id"], row["target_type"], row["target_id"]) == (actor, "user", target)
    assert row["request_id"] == response.headers["x-request-id"] and row["before_json"] is None
    assert "PRIVATE_EVAL_BODY" not in json.dumps(row)


def _identity(role):
    return (LEGACY_OWNER_USER_ID, LEGACY_OWNER_USER_ID) if role == "owner" else ("eval089-admin", TARGET)


@pytest.mark.parametrize("role", ("owner", "admin"))
def test_ablation_http_real_search_repeat_and_scoped_receipt(eval_http, role):
    client, store, current, headers = eval_http
    actor, target = _identity(role)
    found = _store(store, target, "Atlasquasar PostgreSQL PRIVATE_EVAL_BODY", "Atlasquasar")
    _store(store, OTHER, "Atlasquasar PostgreSQL FOREIGN_PRIVATE_EVAL_BODY", "Atlasquasar")
    store.add_eval_case(target, "Atlasquasar", [found])
    store.kv_set("eval:last_run:" + target, '{"retained_baseline":true}')
    before = _snapshot(store)
    first = None
    for _ in range(2):
        audit = _audits(store, "ablation")
        response = client.post(
            "/api/admin/eval/ablation",
            headers=headers[role],
            json={"user_id": target, "signals": ["usage", "usage", "feedback", "unknown"]},
        )
        assert response.status_code == 200
        assert response.json()["user_id"] == target
        report = response.json()["report"]
        assert (report["k"], report["cases"], report["baseline_recall_at_k"], report["underpowered"]) == (
            current.eval_k,
            1,
            1.0,
            True,
        )
        assert report["min_cases_for_a_verdict"] == 12 and report["reason"]
        ranked = report["ranked"]
        assert [r["signal"] for r in ranked] == ["feedback", "usage"]
        assert all(r["verdict"] == "insufficient_evidence" for r in ranked)
        assert all(r["delta_recall"] == r["delta_mrr"] == 0 for r in ranked)
        assert json.loads(store.kv_get("eval:ablation:" + target)) == ranked
        assert store.kv_get("eval:last_run:" + target) == '{"retained_baseline":true}'
        assert store.kv_get("eval:last_run:" + OTHER) == '{"foreign_baseline":true}'
        assert store.kv_get("eval:ablation:" + OTHER) is None
        assert _snapshot(store) == before
        _audit_delta(store, "ablation", audit, response, actor, target)
        if first is None:
            first = report
        else:
            assert report == first


@pytest.mark.parametrize("role", ("owner", "admin"))
def test_chunk_ab_http_real_index_search_gain_and_scoped_delta(eval_http, role):
    client, store, current, headers = eval_http
    actor, target = _identity(role)
    tuned = _embeddings_settings(current, embeddings_chunk_chars=1200)
    vector_boundary = _FakeTopicEmbeddings(tuned)
    article = _make_ko(store, target, _long_import(), title="Импорт", summary="Длинный импорт")
    foreign = _make_ko(store, OTHER, _long_import(), title="Импорт", summary="Длинный импорт")
    # Real one-shot indexer, no background workers or network model calls.
    manager = WorkersManager(tuned, store, None, None, embeddings=vector_boundary)
    client.portal.call(manager._embeddings_index_all)  # noqa: SLF001
    assert store.count_knowledge_chunk_embeddings(target) > 1
    store.add_eval_case(target, QUERY, [article["id"]])
    store.add_eval_case(OTHER, "foreign query", [foreign["id"]])
    store.kv_set("eval:last_run:" + target, '{"retained_baseline":true}')
    before = _snapshot(store)
    # The HTTP route reads these actual state fields; restore before app shutdown.
    old_settings, old_embeddings = client.app.state.settings, client.app.state.embeddings
    client.app.state.settings, client.app.state.embeddings = tuned, vector_boundary
    first = None
    try:
        for _ in range(2):
            calls_before = len(vector_boundary.calls)
            audit = _audits(store, "chunk-ab")
            response = client.post(
                "/api/admin/eval/chunk-ab", headers=headers[role], json={"user_id": target}
            )
            assert response.status_code == 200
            assert response.json()["user_id"] == target
            report = response.json()["report"]
            assert report["k"] == 10 and report["cases"] == 1
            assert report["baseline"]["recall_at_k"] == 0.0
            assert report["chunked"]["recall_at_k"] == 1.0
            assert report["delta"] == {"recall_at_k": 1.0, "precision_at_k": 0.1, "mrr": 1.0}
            for arm, hit in (("baseline", 0), ("chunked", 1)):
                rows = report[arm]["per_case"]
                assert len(rows) == 1 and rows[0]["query"] == QUERY
                assert rows[0]["found"] == hit and rows[0]["expected"] == 1
            assert len(vector_boundary.calls) > calls_before
            assert json.loads(store.kv_get("eval:chunk_ab:" + target)) == report["delta"]
            assert store.kv_get("eval:chunk_ab:" + OTHER) is None
            assert store.kv_get("eval:last_run:" + target) == '{"retained_baseline":true}'
            assert store.kv_get("eval:last_run:" + OTHER) == '{"foreign_baseline":true}'
            assert _snapshot(store) == before
            _audit_delta(store, "chunk-ab", audit, response, actor, target)
            if first is None:
                first = report
            else:
                assert report == first
    finally:
        client.app.state.settings, client.app.state.embeddings = old_settings, old_embeddings


@pytest.mark.parametrize("path", PATHS)
def test_eval_comparison_http_empty_gold_refuses_a_metric(eval_http, path):
    client, store, _, headers = eval_http
    before, audits = _snapshot(store), _audits(store, path)
    response = client.post("/api/admin/eval/" + path, headers=headers["owner"], json={"user_id": TARGET})
    assert response.status_code == 200
    assert response.json() == {"user_id": TARGET, "report": {"cases": 0, "reason": "no gold cases"}}
    assert _snapshot(store) == before
    assert store.kv_get("eval:ablation:" + TARGET) is None and store.kv_get("eval:chunk_ab:" + TARGET) is None
    _audit_delta(store, path, audits, response, LEGACY_OWNER_USER_ID, TARGET)


@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("role,status", (("anonymous", 401), ("user", 403)))
def test_eval_comparison_http_auth_refuses_before_engine(eval_http, monkeypatch, path, role, status):
    from friday import eval as engine

    client, store, _, headers = eval_http
    calls = []

    async def forbidden(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("unauthorized eval engine call")

    name = "compare_signal_ablation" if path == "ablation" else "compare_chunk_recall"
    monkeypatch.setattr(engine, name, forbidden)
    before, audits = _snapshot(store), _audits(store, path)
    response = client.post("/api/admin/eval/" + path, headers=headers[role], json={"user_id": TARGET})
    assert response.status_code == status and not calls
    assert _snapshot(store) == before and _audits(store, path) == audits


@pytest.mark.parametrize(
    "path,reason", (("ablation", "no ablatable signals"), ("chunk-ab", "chunking disabled"))
)
def test_eval_comparison_http_disabled_arm_keeps_prior_receipts(eval_http, path, reason):
    client, store, current, headers = eval_http
    target = _store(store, TARGET, "Atlasquasar PRIVATE_EVAL_BODY", "Atlasquasar")
    store.add_eval_case(TARGET, "Atlasquasar", [target])
    prefix = "eval:ablation:" if path == "ablation" else "eval:chunk_ab:"
    store.kv_set(prefix + TARGET, '{"retained":true}')
    before, audits = _snapshot(store), _audits(store, path)
    old_settings = client.app.state.settings
    client.app.state.settings = replace(current, embeddings_chunk_chars=0)
    try:
        response = client.post(
            "/api/admin/eval/" + path,
            headers=headers["owner"],
            json={"user_id": TARGET, "signals": ["unknown"]},
        )
    finally:
        client.app.state.settings = old_settings
    assert response.status_code == 200
    assert response.json() == {"user_id": TARGET, "report": {"cases": 1, "reason": reason}}
    assert store.kv_get(prefix + TARGET) == '{"retained":true}' and _snapshot(store) == before
    _audit_delta(store, path, audits, response, LEGACY_OWNER_USER_ID, TARGET)
