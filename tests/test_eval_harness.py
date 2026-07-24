"""Retrieval eval harness — §7: quality stops being a feeling.

Pins the pure metrics (recall@k / precision@k / MRR), the gold-set storage, a
run over the real hybrid searcher that actually finds seeded knowledge,
regression detection against the previous run, and the admin endpoints
(cases CRUD + label-from-results search + run).
"""

from __future__ import annotations

import hashlib

import pytest
from fastapi.testclient import TestClient

from jericho.eval import precision_at_k, recall_at_k, reciprocal_rank, run_eval
from jericho.permissions import LEGACY_OWNER_USER_ID
from jericho.server import create_app
from jericho.storage.models import KnowledgeObject, RawObject, new_id

# --- pure metrics ---------------------------------------------------------


def test_metric_functions():
    retrieved = ["a", "b", "c", "d"]
    expected = {"b", "d", "z"}
    assert recall_at_k(retrieved, expected, 4) == pytest.approx(2 / 3)
    assert recall_at_k(retrieved, expected, 1) == 0.0  # only "a" in top-1
    assert precision_at_k(retrieved, expected, 2) == 0.5  # "a","b" -> 1 hit
    assert reciprocal_rank(retrieved, expected) == 0.5  # first hit at rank 2
    assert reciprocal_rank(["x", "y"], expected) == 0.0


# --- storage gold set -----------------------------------------------------


def test_add_list_delete_eval_case(storage):
    storage.ensure_user("alice")
    case = storage.add_eval_case("alice", "  ip   atlas ", ["ko_1", "ko_2", "ko_1"], note="dupes")
    assert case["query"] == "ip atlas"  # normalised
    assert case["expected_ids"] == ["ko_1", "ko_2"]  # deduped, sorted

    # Same query upserts rather than duplicating.
    storage.add_eval_case("alice", "ip atlas", ["ko_3"])
    cases = storage.list_eval_cases("alice")
    assert len(cases) == 1
    assert cases[0]["expected_ids"] == ["ko_3"]

    with pytest.raises(ValueError, match="query is required"):
        storage.add_eval_case("alice", "   ", ["ko_1"])
    with pytest.raises(ValueError, match="expected"):
        storage.add_eval_case("alice", "another", [])

    assert storage.delete_eval_case("alice", cases[0]["id"]) is True
    assert storage.list_eval_cases("alice") == []


# --- run over the real searcher -------------------------------------------


def _store(storage, user_id: str, content: str, title: str) -> str:
    raw = RawObject(
        id=new_id("raw"),
        user_id=user_id,
        source="test",
        source_ref=new_id("src"),
        raw_content=content,
        content_type="text",
        content_hash=hashlib.sha256(content.encode()).hexdigest(),
    )
    storage.store_raw_object(raw)
    ko = KnowledgeObject(
        id=new_id("ko"),
        user_id=user_id,
        raw_object_id=raw.id,
        content=content,
        content_type="text",
        title=title,
        summary=content,
    )
    storage.store_knowledge_object(ko)
    return ko.id


@pytest.mark.asyncio
async def test_run_eval_measures_and_detects_regression(settings, storage):
    storage.ensure_user("alice")
    target = _store(storage, "alice", "Сервер Atlas имеет IP 10.0.0.7 в дата-центре.", "Atlas IP")
    _store(storage, "alice", "Отпуск запланирован на декабрь.", "Отпуск")
    storage.add_eval_case("alice", "IP сервера Atlas", [target])

    report = await run_eval(storage, None, settings, "alice", k=5)
    assert report["cases"] == 1
    assert report["recall_at_k"] == 1.0  # the seeded record is found
    assert report["per_case"][0]["found"] == 1
    assert report["regression"]["previous_recall"] is None  # first run, no baseline

    # A second gold case the searcher cannot satisfy drops recall — a regression
    # relative to the stored first run.
    storage.add_eval_case("alice", "несуществующая тема без совпадений xyzzy", ["ko_missing"])
    second = await run_eval(storage, None, settings, "alice", k=5)
    assert second["recall_at_k"] < 1.0
    assert second["regression"]["previous_recall"] == 1.0
    assert second["regression"]["regressed"] is True


@pytest.mark.asyncio
async def test_run_eval_empty_gold_set(settings, storage):
    storage.ensure_user("alice")
    report = await run_eval(storage, None, settings, "alice")
    assert report == {"cases": 0, "recall_at_k": None, "reason": "no gold cases"}


# --- admin endpoints ------------------------------------------------------


def test_eval_endpoints_end_to_end(settings):
    app = create_app(settings)
    with TestClient(app) as client:
        owner = {"Authorization": f"Bearer {settings.api_token}"}
        target = _store(app.state.storage, LEGACY_OWNER_USER_ID, "Проект Orion на PostgreSQL 16.", "Orion")

        # Label-from-results: search surfaces the record to pick.
        found = client.get("/api/admin/eval/search", params={"q": "Orion PostgreSQL"}, headers=owner)
        assert found.status_code == 200
        assert target in [item["id"] for item in found.json()["items"]]

        added = client.post(
            "/api/admin/eval/cases",
            json={"query": "Orion база данных", "expected_ids": [target], "note": "t"},
            headers=owner,
        )
        assert added.status_code == 200

        listed = client.get("/api/admin/eval/cases", headers=owner)
        assert listed.json()["count"] == 1

        run = client.post("/api/admin/eval/run", json={}, headers=owner)
        assert run.status_code == 200
        assert run.json()["report"]["recall_at_k"] == 1.0

        case_id = listed.json()["items"][0]["id"]
        deleted = client.delete(
            f"/api/admin/eval/cases/{case_id}", params={"user_id": LEGACY_OWNER_USER_ID}, headers=owner
        )
        assert deleted.status_code == 200
        assert client.get("/api/admin/eval/cases", headers=owner).json()["count"] == 0

        actions = [row["action"] for row in app.state.storage.list_audit_log(None, limit=50)]
        assert {"admin.eval.case_add", "admin.eval.run", "admin.eval.case_delete"} <= set(actions)
