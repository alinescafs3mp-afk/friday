"""Lifecycle and conflict admin HTTP: stages, pages, persist, skips, authority."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime

import httpx
import pytest
from fastapi.testclient import TestClient

from friday.permissions import LEGACY_OWNER_USER_ID
from friday.server import create_app
from friday.storage._base import unpack_snapshot
from friday.storage.models import KnowledgeObject, RawObject, new_id
from tests.test_api_tokens import _issue
from tests.test_release_1_0_conversation_oracles import _audit, _body, _equal
from tests.test_release_1_0_knowledge_read_oracles import _read_state as _knowledge_read_state

_A, _B = "local:r10-lc-a", "local:r10-lc-b"
_ADMIN, _ORDINARY = "local:r10-lc-admin", "local:r10-lc-ordinary"
_CANARY, _FOREIGN = "PRIVATE_LC_CANARY_17", "FOREIGN_PRIVATE_LC"
_STALE = "2020-01-01T00:00:00+00:00"
_ACTIVE = {"active": 12, "archived": 1, "deprecated": 1, "deleted": 0}
_OMIT = {
    "apply_audit": "admin.lifecycle.archive",
    "apply_lower_audit": "admin.lifecycle.lower_importance",
    "apply_keep_audit": "admin.lifecycle.keep",
    "conflict_read_audit": "admin.conflicts.read",
    "bulk_audit": "admin.knowledge_conflict.dismissed",
}
_REWRITE = {
    "apply_keep_wrong": "admin.lifecycle.keep",
    "bulk_wrong": "admin.knowledge_conflict.dismissed",
}


def _ko(
    storage,
    user_id,
    title,
    *,
    importance=0.5,
    quality=0.5,
    promotion=0.5,
    stage="active",
    updated=None,
    content=None,
):
    body = title if content is None else content
    raw = RawObject(
        id=new_id("raw"),
        user_id=user_id,
        source="test",
        source_ref=new_id("src"),
        raw_content=body,
        content_type="text",
        content_hash=hashlib.sha256(body.encode()).hexdigest(),
    )
    storage.store_raw_object(raw)
    ko = KnowledgeObject(
        id=new_id("ko"),
        user_id=user_id,
        raw_object_id=raw.id,
        content=body,
        content_type="text",
        title=title,
        summary=title,
        importance=importance,
        quality_score=quality,
        promotion_score=promotion,
        lifecycle_stage=stage,
    )
    storage.store_knowledge_object(ko)
    if updated:
        storage.execute("UPDATE knowledge_objects SET updated_at=? WHERE id=?", (updated, ko.id))
        storage.commit()
    return ko.id


def _meta(row):
    value = row["metadata_json"]
    return json.loads(value) if isinstance(value, str) else value


def _table(ctx, name):
    return [dict(r) for r in ctx["storage"].execute(f"SELECT * FROM {name} ORDER BY id").fetchall()]


def _knowledge(ctx, user_id=None):
    rows = [dict(r) for r in ctx["storage"].execute("SELECT * FROM knowledge_objects ORDER BY id").fetchall()]
    return [row for row in rows if user_id is None or row["user_id"] == user_id]


def _conflicts(ctx):
    return [
        dict(r) for r in ctx["storage"].execute("SELECT * FROM knowledge_conflicts ORDER BY id").fetchall()
    ]


def _history(ctx):
    return [
        dict(r)
        for r in ctx["storage"]
        .execute("SELECT * FROM knowledge_object_versions ORDER BY knowledge_object_id, version, id")
        .fetchall()
    ]


def _graph(ctx):
    return (_table(ctx, "entities"), _table(ctx, "relations"))


def _side(ctx):
    return (_table(ctx, "raw_objects"), _graph(ctx), _history(ctx))


def _read_state(ctx):
    result = _knowledge_read_state(ctx)
    result["knowledge_conflicts"] = [
        dict(r) for r in ctx["storage"].execute("SELECT * FROM knowledge_conflicts ORDER BY rowid").fetchall()
    ]
    return result


def _state_except(before, after, skip, code):
    _equal(
        {key: after[key] for key in after if key not in skip},
        {key: before[key] for key in before if key not in skip},
        code,
    )


def _row_snapshot(row):
    return json.loads(json.dumps(dict(row), ensure_ascii=False, sort_keys=True, default=str))


def _all_audits(ctx):
    return [dict(r) for r in ctx["storage"].execute("SELECT * FROM audit_log ORDER BY rowid").fetchall()]


def _no_foreign(response):
    assert _FOREIGN not in response.text, "lc_conflict_no_foreign"


def _audit_private(ctx):
    blob = json.dumps(_all_audits(ctx), ensure_ascii=False)
    assert _CANARY not in blob and _FOREIGN not in blob, "lc_audit_privacy"
    return blob


def _bounded(value, started, code):
    stamp = datetime.fromisoformat(value)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    assert started.replace(microsecond=0) <= stamp <= datetime.now(UTC), code


def _closed(before, after, changes, clocks, started, code, json_fields=()):
    expected = dict(before)
    expected.update(changes)
    for field in json_fields:
        actual, wanted = after[field], expected[field]
        parsed_actual = json.loads(actual) if isinstance(actual, str) else actual
        parsed_wanted = json.loads(wanted) if isinstance(wanted, str) else wanted
        _equal(parsed_actual, parsed_wanted, code)
        expected[field] = actual
    for field in clocks:
        _bounded(after[field], started, code)
        expected[field] = after[field]
    _equal(after, expected, code)


def _fresh(storage, action, prior, code="lc_audit_prefix"):
    rows = _audit(storage, action)
    _equal(rows[: len(prior)], prior, code)
    return rows[len(prior) :]


def _actors(rows, target_type, targets, code):
    _equal(
        [(row["user_id"], row["target_type"], row["target_id"]) for row in rows],
        [(LEGACY_OWNER_USER_ID, target_type, target) for target in targets],
        code,
    )


def _snap(value):
    if isinstance(value, (bytes, bytearray, memoryview)):
        value = unpack_snapshot(bytes(value))
    elif isinstance(value, str) and value.startswith("zKOV1"):
        value = unpack_snapshot(value.encode("latin-1"))
    return json.loads(value)


def _new_history(before, after, changed_ids, by_after, started, code):
    old = {row["id"] for row in before}
    _equal([row for row in after if row["id"] in old], before, code)
    new_rows = [row for row in after if row["id"] not in old]
    _equal(sorted(row["knowledge_object_id"] for row in new_rows), sorted(changed_ids), code)
    for row in new_rows:
        _bounded(row["created_at"], started, code)
        snap = _snap(row["snapshot_json"])
        ko = by_after[row["knowledge_object_id"]]
        _equal(
            (row["user_id"], row["knowledge_object_id"], row["version"]),
            (ko["user_id"], ko["id"], ko["version"]),
            code,
        )
        _equal(snap, _row_snapshot(ko), code)


@pytest.fixture
def lc_http(settings):
    assert not settings.llm_enabled and not settings.workers_enabled
    app = create_app(replace(settings, shared_archive=True, telegram_owner_chat_ids=[]))
    with TestClient(app) as client:
        storage = app.state.storage
        owner_secret = "jrc_synthetic_lc_owner"
        _issue(storage, LEGACY_OWNER_USER_ID, "owner", owner_secret)
        ctx = {
            "client": client,
            "storage": storage,
            "owner": {"Authorization": "Bearer " + owner_secret},
            "headers": {},
        }
        for key, person, preset in (
            ("a", _A, "user"),
            ("b", _B, "user"),
            ("admin", _ADMIN, "admin"),
            ("ordinary", _ORDINARY, "user"),
        ):
            secret = "jrc_synthetic_lc_" + key
            _issue(storage, person, preset, secret)
            ctx["headers"][key] = {"Authorization": "Bearer " + secret}
        stale = [
            _ko(
                storage,
                _A,
                f"Устаревшая {index}",
                importance=importance,
                quality=0.2,
                promotion=0.2,
                updated=_STALE,
                content=f"{_CANARY} stale {index}",
            )
            for index, importance in enumerate((0.10, 0.12, 0.14, 0.16, 0.30))
        ]
        ctx["stale"] = stale
        ctx["fresh"] = _ko(
            storage, _A, "Свежая", importance=0.9, quality=0.9, promotion=0.9, content=f"{_CANARY} fresh"
        )
        ctx["archived_seed"] = _ko(storage, _A, "Уже архив", stage="archived")
        ctx["deprecated_seed"] = _ko(storage, _A, "Уже погашен", stage="deprecated")
        ctx["foreign"] = _ko(
            storage,
            _B,
            _FOREIGN,
            importance=0.1,
            quality=0.2,
            promotion=0.2,
            updated=_STALE,
            content=_FOREIGN,
        )
        conflict_ids = []
        for index, confidence in enumerate((0.95, 0.85, 0.75)):
            left = _ko(
                storage,
                _A,
                f"Конфликт A{index}",
                importance=0.9,
                quality=0.9,
                promotion=0.9,
                content=f"{_CANARY} ca{index}",
            )
            right = _ko(
                storage,
                _A,
                f"Конфликт B{index}",
                importance=0.9,
                quality=0.9,
                promotion=0.9,
                content=f"{_CANARY} cb{index}",
            )
            row = storage.store_knowledge_conflict(
                _A,
                left,
                right,
                conflict_type="address_mismatch",
                confidence=confidence,
                evidence={"secret": _CANARY},
            )
            conflict_ids.append(row["id"])
        ctx["conflicts"] = conflict_ids
        left = _ko(
            storage, _B, f"{_FOREIGN} fa", importance=0.9, quality=0.9, promotion=0.9, content=_FOREIGN
        )
        right = _ko(
            storage, _B, f"{_FOREIGN} fb", importance=0.9, quality=0.9, promotion=0.9, content=_FOREIGN
        )
        storage.store_knowledge_conflict(
            _B, left, right, conflict_type="address_mismatch", confidence=0.99, evidence={"secret": _FOREIGN}
        )
        assert storage.count_lifecycle_candidates(_A) == 5
        yield ctx


def _assert_stages(ctx):
    before, prior = _read_state(ctx), _audit(ctx["storage"], "admin.lifecycle.read")
    response = ctx["client"].get("/api/admin/lifecycle", headers=ctx["owner"], params={"user_id": _A})
    body = _body(response)
    _no_foreign(response)
    _equal(body["user_id"], _A, "lc_stages_user")
    _equal(body["stages"], _ACTIVE, "lc_stages")
    _equal(_read_state(ctx), before, "lc_read_state")
    rows = [row for row in _audit(ctx["storage"], "admin.lifecycle.read") if row["target_id"] == _A]
    _equal(len(rows) - len([row for row in prior if row["target_id"] == _A]), 1, "lc_stages_audit_count")
    _equal(
        (rows[-1]["user_id"], rows[-1]["target_type"]),
        (LEGACY_OWNER_USER_ID, "user"),
        "lc_stages_audit_actor",
    )
    _audit_private(ctx)


def test_lifecycle_stats_literal_stages_and_audit(lc_http):
    _assert_stages(lc_http)


def _assert_conflicts(ctx):
    client, owner, storage = ctx["client"], ctx["owner"], ctx["storage"]
    expected = ctx["conflicts"]
    before, prior_all, prior_read = (
        _read_state(ctx),
        _all_audits(ctx),
        _audit(storage, "admin.conflicts.read"),
    )
    pages = []
    for offset in range(4):
        response = client.get(
            "/api/admin/conflicts",
            headers=owner,
            params={"user_id": _A, "status": "suggested", "limit": 1, "offset": offset},
        )
        body = _body(response)
        _no_foreign(response)
        _equal(
            {key: body[key] for key in ("user_id", "count", "total", "limit", "offset", "matched_at_least")},
            {
                "user_id": _A,
                "count": 1 if offset < 3 else 0,
                "total": 3,
                "limit": 1,
                "offset": offset,
                "matched_at_least": 3,
            },
            "lc_conflict_total",
        )
        _equal(body["truncated"], offset + body["count"] < 3, "lc_conflict_page")
        pages.append([item["id"] for item in body["items"]])
        if offset < 3:
            item = body["items"][0]
            _equal(
                (item["id"], item["status"], item["conflict_type"], item["knowledge_a_title"]),
                (expected[offset], "suggested", "address_mismatch", f"Конфликт A{offset}"),
                "lc_conflict_members",
            )
            assert _CANARY not in json.dumps(item, ensure_ascii=False)
    _equal(pages, [[expected[0]], [expected[1]], [expected[2]], []], "lc_conflict_members")
    bad = client.get("/api/admin/conflicts", headers=owner, params={"user_id": _A, "status": "nope"})
    _equal(bad.status_code, 400, "lc_conflict_invalid_status")
    _equal(_read_state(ctx), before, "lc_read_state")
    _equal(_all_audits(ctx)[: len(prior_all)], prior_all, "lc_audit_prefix")
    _actors(_fresh(storage, "admin.conflicts.read", prior_read), "user", [_A] * 5, "lc_conflict_read_audit")
    _audit_private(ctx)


def test_conflicts_list_exact_members_pages_count_total(lc_http):
    _assert_conflicts(lc_http)


def _assert_apply(ctx):
    client, owner, storage = ctx["client"], ctx["owner"], ctx["storage"]
    page0 = _body(
        client.get(
            "/api/admin/lifecycle/candidates", headers=owner, params={"user_id": _A, "limit": 1, "offset": 0}
        )
    )
    page1 = _body(
        client.get(
            "/api/admin/lifecycle/candidates", headers=owner, params={"user_id": _A, "limit": 1, "offset": 1}
        )
    )
    later_id = page1["items"][0]["knowledge_object"]["id"]
    assert later_id != page0["items"][0]["knowledge_object"]["id"], "lc_apply_later_page"
    listed = {
        item["knowledge_object"]["id"]: item
        for item in _body(
            client.get("/api/admin/lifecycle/candidates", headers=owner, params={"user_id": _A, "limit": 500})
        )["items"]
    }
    lower_id = next(key for key, item in listed.items() if item["knowledge_object"]["importance"] == 0.3)
    keep_id = next(key for key in listed if key not in {later_id, lower_id})
    started = datetime.now(UTC)
    before = _knowledge(ctx)
    before_state, before_b, before_c, side = (
        _read_state(ctx),
        _knowledge(ctx, _B),
        _conflicts(ctx),
        _side(ctx),
    )
    prior_all = _all_audits(ctx)
    prior_archive = _audit(storage, "admin.lifecycle.archive")
    prior_lower = _audit(storage, "admin.lifecycle.lower_importance")
    prior_keep = _audit(storage, "admin.lifecycle.keep")
    by_before = {row["id"]: row for row in before}
    version_before = by_before[later_id]["version"]
    archive_response = client.post(
        "/api/admin/lifecycle/apply",
        headers=owner,
        json={"user_id": _A, "knowledge_ids": [later_id], "action": "archive"},
    )
    archived = _body(archive_response)
    _no_foreign(archive_response)
    _equal(
        (archived["action"], archived["changed_count"], archived["skipped"]),
        ("archive", 1, []),
        "lc_apply_later_page",
    )
    _equal(archived["changed"][0]["id"], later_id, "lc_apply_later_page")
    row = storage.get_knowledge_object(later_id, _A)
    _equal(row["lifecycle_stage"], "archived", "lc_apply_archive_persisted")
    _equal(_knowledge(ctx, _B), before_b, "lc_foreign_untouched")
    _equal(row["version"], version_before + 1, "lc_apply_archive_history")
    _equal(archived["changed"][0]["item"]["lifecycle_stage"], "archived", "lc_apply_archive_http")
    archive_rows = _fresh(storage, "admin.lifecycle.archive", prior_archive)
    _equal(len(archive_rows), 1, "lc_apply_audit_count")
    _actors(archive_rows, "knowledge_object", [later_id], "lc_apply_audit_actor")
    suggested = listed[lower_id]["suggested_importance"]
    lowered = _body(
        client.post(
            "/api/admin/lifecycle/apply",
            headers=owner,
            json={"user_id": _A, "knowledge_ids": [lower_id], "action": "lower_importance"},
        )
    )
    low = storage.get_knowledge_object(lower_id, _A)
    _equal(low["importance"], suggested, "lc_apply_lower_persisted")
    _equal(lowered["changed"][0]["item"]["importance"], suggested, "lc_apply_lower_http")
    _actors(
        _fresh(storage, "admin.lifecycle.lower_importance", prior_lower),
        "knowledge_object",
        [lower_id],
        "lc_apply_lower_audit",
    )
    kept = _body(
        client.post(
            "/api/admin/lifecycle/apply",
            headers=owner,
            json={"user_id": _A, "knowledge_ids": [keep_id], "action": "keep"},
        )
    )
    _equal(
        _meta(storage.get_knowledge_object(keep_id, _A))["lifecycle_review"],
        {"decision": "keep", "reviewed_by": LEGACY_OWNER_USER_ID},
        "lc_apply_keep_persisted",
    )
    _equal(kept["changed_count"], 1, "lc_apply_keep_http")
    _actors(
        _fresh(storage, "admin.lifecycle.keep", prior_keep),
        "knowledge_object",
        [keep_id],
        "lc_apply_keep_audit",
    )
    skipped = _body(
        client.post(
            "/api/admin/lifecycle/apply",
            headers=owner,
            json={
                "user_id": _A,
                "knowledge_ids": [ctx["fresh"], ctx["foreign"], "ko_missing"],
                "action": "archive",
            },
        )
    )
    _equal(
        skipped["skipped"],
        [
            {"id": ctx["fresh"], "reason": "not_a_current_candidate"},
            {"id": ctx["foreign"], "reason": "not_found"},
            {"id": "ko_missing", "reason": "not_found"},
        ],
        "lc_apply_noncandidate",
    )
    _equal(skipped["changed"], [], "lc_apply_wrong_target")
    _equal(
        storage.get_knowledge_object(ctx["fresh"], _A)["lifecycle_stage"], "active", "lc_apply_noncandidate"
    )
    _equal(
        storage.get_knowledge_object(ctx["foreign"], _B)["lifecycle_stage"], "active", "lc_apply_wrong_target"
    )
    changed = {later_id, lower_id, keep_id}
    after_rows = _knowledge(ctx)
    by_after = {row["id"]: row for row in after_rows}
    _closed(
        by_before[later_id],
        by_after[later_id],
        {"lifecycle_stage": "archived", "version": version_before + 1},
        ("updated_at",),
        started,
        "lc_apply_changed_row",
    )
    _closed(
        by_before[lower_id],
        by_after[lower_id],
        {"importance": suggested, "version": by_before[lower_id]["version"] + 1},
        ("updated_at",),
        started,
        "lc_apply_changed_row",
    )
    _closed(
        by_before[keep_id],
        by_after[keep_id],
        {
            "metadata_json": {
                **_meta(by_before[keep_id]),
                "lifecycle_review": {"decision": "keep", "reviewed_by": LEGACY_OWNER_USER_ID},
            },
            "version": by_before[keep_id]["version"] + 1,
        },
        ("updated_at",),
        started,
        "lc_apply_changed_row",
        json_fields=("metadata_json",),
    )
    _equal(
        [row for row in after_rows if row["id"] not in changed],
        [row for row in before if row["id"] not in changed],
        "lc_apply_preserves_others",
    )
    _equal(_knowledge(ctx, _B), before_b, "lc_foreign_untouched")
    _equal(_conflicts(ctx), before_c, "lc_apply_preserves_conflicts")
    raw, graph, hist = _side(ctx)
    _equal((raw, graph), (side[0], side[1]), "lc_apply_preserves_side")
    _new_history(side[2], hist, changed, by_after, started, "lc_apply_history")
    _state_except(
        before_state,
        _read_state(ctx),
        ("knowledge_objects", "knowledge_object_versions"),
        "lc_apply_preserves_state",
    )
    _equal(_all_audits(ctx)[: len(prior_all)], prior_all, "lc_audit_prefix")
    empty = client.post(
        "/api/admin/lifecycle/apply",
        headers=owner,
        json={"user_id": _A, "knowledge_ids": [], "action": "archive"},
    )
    _equal(empty.status_code, 400, "lc_apply_empty_ids")
    bad = client.post(
        "/api/admin/lifecycle/apply",
        headers=owner,
        json={"user_id": _A, "knowledge_ids": [keep_id], "action": "explode"},
    )
    _equal(bad.status_code, 400, "lc_apply_bad_action")
    _audit_private(ctx)


def test_apply_archive_lower_keep_changes_and_skips(lc_http):
    _assert_apply(lc_http)


def _assert_review(ctx):
    client, owner, storage = ctx["client"], ctx["owner"], ctx["storage"]
    confirm_id, first_bulk, second_bulk = ctx["conflicts"]
    started = datetime.now(UTC)
    before_k, before_b, before_c, side = _knowledge(ctx), _knowledge(ctx, _B), _conflicts(ctx), _side(ctx)
    before_state = _read_state(ctx)
    prior_all = _all_audits(ctx)
    prior_confirmed = _audit(storage, "admin.knowledge_conflict.confirmed")
    prior_dismissed = _audit(storage, "admin.knowledge_conflict.dismissed")
    by_before = {row["id"]: row for row in before_c}
    body = _body(
        client.post(
            f"/api/admin/conflicts/{confirm_id}/review",
            headers=owner,
            json={"user_id": _A, "status": "confirmed", "resolution_note": "note-private"},
        )
    )
    row = storage.get_knowledge_conflict(_A, confirm_id)
    _equal(row["status"], "confirmed", "lc_review_persisted")
    _equal((body["item"]["id"], body["item"]["status"]), (confirm_id, "confirmed"), "lc_review_http")
    assert "note-private" not in json.dumps(body, ensure_ascii=False)
    _equal(
        storage.get_knowledge_object(row["knowledge_a_id"], _A)["lifecycle_stage"],
        "active",
        "lc_review_preserves_knowledge",
    )
    _actors(
        _fresh(storage, "admin.knowledge_conflict.confirmed", prior_confirmed),
        "knowledge_conflict",
        [confirm_id],
        "lc_review_audit",
    )
    missing = client.post(
        "/api/admin/conflicts/conf_missing/review",
        headers=owner,
        json={"user_id": _A, "status": "confirmed"},
    )
    _equal(missing.status_code, 404, "lc_review_missing")
    bad = client.post(
        f"/api/admin/conflicts/{first_bulk}/review",
        headers=owner,
        json={"user_id": _A, "status": "nope"},
    )
    _equal(bad.status_code, 400, "lc_review_bad_status")
    _equal(storage.get_knowledge_conflict(_A, first_bulk)["status"], "suggested", "lc_invalid_no_effect")
    bulk = _body(
        client.post(
            "/api/admin/conflicts/bulk-review",
            headers=owner,
            json={
                "user_id": _A,
                "status": "dismissed",
                "conflict_ids": [first_bulk, second_bulk, "conf_missing"],
            },
        )
    )
    _equal(bulk["changed_count"], 2, "lc_bulk_changed_count")
    _equal(
        sorted(item["id"] for item in bulk["changed"]),
        sorted([first_bulk, second_bulk]),
        "lc_bulk_changed_ids",
    )
    _equal(bulk["skipped"], [{"id": "conf_missing", "reason": "not_found"}], "lc_bulk_skip")
    _equal(storage.get_knowledge_conflict(_A, first_bulk)["status"], "dismissed", "lc_bulk_persisted")
    _equal(storage.get_knowledge_conflict(_A, second_bulk)["status"], "dismissed", "lc_bulk_persisted")
    _equal(
        storage.get_knowledge_conflict(_A, confirm_id)["status"],
        "confirmed",
        "lc_bulk_preserves_other_status",
    )
    _actors(
        _fresh(storage, "admin.knowledge_conflict.dismissed", prior_dismissed),
        "knowledge_conflict",
        [first_bulk, second_bulk],
        "lc_bulk_audit",
    )
    empty = client.post(
        "/api/admin/conflicts/bulk-review",
        headers=owner,
        json={"user_id": _A, "status": "dismissed", "conflict_ids": []},
    )
    _equal(empty.status_code, 400, "lc_bulk_empty")
    illegal = client.post(
        "/api/admin/conflicts/bulk-review",
        headers=owner,
        json={"user_id": _A, "status": "suggested", "conflict_ids": [confirm_id]},
    )
    _equal(illegal.status_code, 400, "lc_bulk_bad_status")
    _equal(storage.get_knowledge_conflict(_A, confirm_id)["status"], "confirmed", "lc_invalid_no_effect")
    changed = {confirm_id, first_bulk, second_bulk}
    after_c = _conflicts(ctx)
    by_after = {row["id"]: row for row in after_c}
    _closed(
        by_before[confirm_id],
        by_after[confirm_id],
        {"status": "confirmed", "reviewed_by": LEGACY_OWNER_USER_ID, "resolution_note": "note-private"},
        ("reviewed_at",),
        started,
        "lc_review_changed_row",
    )
    for cid in (first_bulk, second_bulk):
        _closed(
            by_before[cid],
            by_after[cid],
            {"status": "dismissed", "reviewed_by": LEGACY_OWNER_USER_ID, "resolution_note": ""},
            ("reviewed_at",),
            started,
            "lc_review_changed_row",
        )
    _equal(_knowledge(ctx), before_k, "lc_review_preserves_knowledge")
    _equal(_knowledge(ctx, _B), before_b, "lc_foreign_untouched")
    _equal(
        [row for row in after_c if row["id"] not in changed],
        [row for row in before_c if row["id"] not in changed],
        "lc_review_preserves_others",
    )
    _equal(_side(ctx), side, "lc_review_preserves_side")
    _state_except(before_state, _read_state(ctx), ("knowledge_conflicts",), "lc_review_preserves_state")
    _equal(_all_audits(ctx)[: len(prior_all)], prior_all, "lc_audit_prefix")
    _audit_private(ctx)


def test_review_and_bulk_review_persisted_statuses_and_skips(lc_http):
    _assert_review(lc_http)


def _assert_authority(ctx):
    client = ctx["client"]
    before, prior_all = _read_state(ctx), _all_audits(ctx)
    cid = ctx["conflicts"][0]
    reads = [
        ("GET", "/api/admin/lifecycle", {"user_id": _A}),
        ("GET", "/api/admin/lifecycle/candidates", {"user_id": _A}),
        ("GET", "/api/admin/conflicts", {"user_id": _A}),
    ]
    mutes = [
        (
            "POST",
            "/api/admin/lifecycle/apply",
            {"user_id": _A, "knowledge_ids": ctx["stale"][:1], "action": "archive"},
        ),
        ("POST", "/api/admin/lifecycle/deprecate", {"user_id": _A, "ids": ctx["stale"][:1]}),
        ("POST", f"/api/admin/conflicts/{cid}/review", {"user_id": _A, "status": "confirmed"}),
        ("POST", f"/api/admin/conflicts/{cid}/resolve", {"user_id": _A, "winner_id": ctx["stale"][0]}),
        (
            "POST",
            "/api/admin/conflicts/bulk-review",
            {"user_id": _A, "status": "confirmed", "conflict_ids": [cid]},
        ),
    ]
    for headers, status in (({}, 401), (ctx["headers"]["ordinary"], 403)):
        for method, url, payload in [*[(m, u, p) for m, u, p in reads], *mutes]:
            kwargs = {"headers": headers, "params" if method == "GET" else "json": payload}
            response = client.request(method, url, **kwargs)
            _equal(response.status_code, status, "lc_authority_refusal")
            _no_foreign(response)
    owner_mutes = [
        (
            "POST",
            "/api/admin/lifecycle/apply",
            {"user_id": LEGACY_OWNER_USER_ID, "knowledge_ids": ctx["stale"][:1], "action": "archive"},
        ),
        (
            "POST",
            "/api/admin/lifecycle/deprecate",
            {"user_id": LEGACY_OWNER_USER_ID, "ids": ctx["stale"][:1]},
        ),
        (
            "POST",
            f"/api/admin/conflicts/{cid}/review",
            {"user_id": LEGACY_OWNER_USER_ID, "status": "confirmed"},
        ),
        (
            "POST",
            f"/api/admin/conflicts/{cid}/resolve",
            {"user_id": LEGACY_OWNER_USER_ID, "winner_id": ctx["stale"][0]},
        ),
        (
            "POST",
            "/api/admin/conflicts/bulk-review",
            {"user_id": LEGACY_OWNER_USER_ID, "status": "confirmed", "conflict_ids": [cid]},
        ),
    ]
    for method, url, payload in owner_mutes:
        response = client.request(method, url, headers=ctx["headers"]["admin"], json=payload)
        _equal(response.status_code, 403, "lc_owner_protection")
    _equal(_read_state(ctx), before, "lc_refusal_state")
    after_audits = _all_audits(ctx)
    _equal(after_audits[: len(prior_all)], prior_all, "lc_audit_prefix")
    fresh = after_audits[len(prior_all) :]
    _equal(
        [(row["user_id"], row["action"], row["target_type"]) for row in fresh],
        [("anonymous", "auth.failed", "auth")] * 8,
        "lc_authority_no_audit",
    )
    assert all(not str(row["action"]).startswith("admin.") for row in fresh), "lc_authority_no_audit"
    _audit_private(ctx)


def test_all_eight_routes_authority_target_refusal(lc_http):
    _assert_authority(lc_http)


_FAULTS = [
    ("stages_count", "lc_stages", "stages"),
    ("conflict_members", "lc_conflict_members", "conflict"),
    ("conflict_total", "lc_conflict_total", "conflict"),
    ("conflict_read_audit", "lc_conflict_read_audit", "conflict"),
    ("apply_dropped", "lc_apply_archive_persisted", "apply"),
    ("apply_collateral", "lc_apply_preserves_others", "apply"),
    ("foreign_write", "lc_foreign_untouched", "apply"),
    ("apply_audit", "lc_apply_audit_count", "apply"),
    ("apply_lower_audit", "lc_apply_lower_audit", "apply"),
    ("apply_keep_audit", "lc_apply_keep_audit", "apply"),
    ("apply_keep_wrong", "lc_apply_keep_audit", "apply"),
    ("apply_changed_row", "lc_apply_changed_row", "apply"),
    ("review_dropped", "lc_review_persisted", "review"),
    ("bulk_dropped", "lc_bulk_persisted", "review"),
    ("review_row_collateral", "lc_review_changed_row", "review"),
    ("bulk_audit", "lc_bulk_audit", "review"),
    ("bulk_wrong", "lc_bulk_audit", "review"),
    ("own_ko", "lc_read_state", "stages"),
    ("refusal_link", "lc_refusal_state", "authority"),
    ("apply_history_content", "lc_apply_history", "apply"),
]


def _fault_row(connection, table, row_id, field, value):
    found = connection.execute(f"SELECT {field} FROM {table} WHERE id=?", (row_id,)).fetchone()
    return found is not None and found[field] == value


def _fault_snapshot_content(raw):
    text = unpack_snapshot(bytes(raw)) if isinstance(raw, (bytes, bytearray, memoryview)) else raw
    return json.loads(text)


@pytest.mark.parametrize("fault,code,scenario_name", _FAULTS, ids=[row[0] for row in _FAULTS])
def test_lifecycle_conflict_oracles_detect_corrupted_http_and_writes(
    lc_http, monkeypatch, fault, code, scenario_name
):
    ctx, storage, original = lc_http, lc_http["storage"], TestClient.request
    real_audit = storage.log_audit
    omit_action = _OMIT.get(fault)
    rewrite_action = _REWRITE.get(fault)
    hits: list[str] = []

    def note() -> None:
        hits.append(fault)

    def gated(entry):
        if omit_action and entry.action == omit_action:
            note()
            return None
        if rewrite_action and entry.action == rewrite_action:
            note()
            return real_audit(replace(entry, target_id="wrong-audit-target"))
        return real_audit(entry)

    if omit_action or rewrite_action:
        monkeypatch.setattr(storage, "log_audit", gated)

    def altered(self, method, url, **kwargs):
        method = method.upper()
        response = original(self, method, url, **kwargs)
        if (
            fault == "stages_count"
            and method == "GET"
            and url == "/api/admin/lifecycle"
            and response.status_code == 200
        ):
            body = response.json()
            body["stages"] = {**body["stages"], "active": body["stages"]["active"] + 1}
            note()
            return httpx.Response(200, json=body, request=response.request)
        if (
            fault == "own_ko"
            and method == "GET"
            and url == "/api/admin/lifecycle"
            and response.status_code == 200
        ):
            with storage.transaction() as connection:
                connection.execute(
                    "UPDATE knowledge_objects SET title='corrupted-own' WHERE id=?", (ctx["fresh"],)
                )
                if _fault_row(connection, "knowledge_objects", ctx["fresh"], "title", "corrupted-own"):
                    note()
        if fault == "refusal_link" and response.status_code == 403 and not ctx.get("_refusal_injected"):
            ctx["_refusal_injected"] = True
            with storage.transaction() as connection:
                connection.execute(
                    "INSERT INTO user_permission_overrides(user_id, security_id, effect, updated_at) VALUES(?,?,?,?)",
                    (_A, "knowledge.read", "deny", "2026-01-01T00:00:00+00:00"),
                )
                found = connection.execute(
                    "SELECT effect FROM user_permission_overrides WHERE user_id=? AND security_id=?",
                    (_A, "knowledge.read"),
                ).fetchone()
                if found is not None and found["effect"] == "deny":
                    note()
        if (
            fault in {"conflict_total", "conflict_members"}
            and method == "GET"
            and url == "/api/admin/conflicts"
            and response.status_code == 200
        ):
            body = response.json()
            if fault == "conflict_total":
                body["total"] = 0
                note()
            elif fault == "conflict_members" and body.get("items"):
                body["items"][0] = {**body["items"][0], "id": "invented-conflict"}
                note()
            return httpx.Response(200, json=body, request=response.request)
        if method == "POST" and url == "/api/admin/lifecycle/apply" and response.status_code == 200:
            payload = response.json()
            if payload.get("changed"):
                target = payload["changed"][0]["id"]
                with storage.transaction() as connection:
                    if fault == "apply_dropped" and payload.get("action") == "archive":
                        connection.execute(
                            "UPDATE knowledge_objects SET lifecycle_stage='active' WHERE id=?", (target,)
                        )
                        if _fault_row(connection, "knowledge_objects", target, "lifecycle_stage", "active"):
                            note()
                    elif fault == "apply_collateral" and payload.get("action") == "archive":
                        connection.execute(
                            "UPDATE knowledge_objects SET title='corrupted-sibling' WHERE id=?",
                            (ctx["archived_seed"],),
                        )
                        if _fault_row(
                            connection,
                            "knowledge_objects",
                            ctx["archived_seed"],
                            "title",
                            "corrupted-sibling",
                        ):
                            note()
                    elif fault == "foreign_write":
                        connection.execute(
                            "UPDATE knowledge_objects SET title='corrupted-foreign' WHERE id=?",
                            (ctx["foreign"],),
                        )
                        if _fault_row(
                            connection, "knowledge_objects", ctx["foreign"], "title", "corrupted-foreign"
                        ):
                            note()
                    elif fault == "apply_changed_row":
                        connection.execute(
                            "UPDATE knowledge_objects SET title='corrupted-inside' WHERE id=?", (target,)
                        )
                        if _fault_row(connection, "knowledge_objects", target, "title", "corrupted-inside"):
                            note()
                    elif fault == "apply_history_content" and payload.get("action") == "archive":
                        row = connection.execute(
                            "SELECT id, snapshot_json FROM knowledge_object_versions "
                            "WHERE knowledge_object_id=? ORDER BY version DESC LIMIT 1",
                            (target,),
                        ).fetchone()
                        if row is not None:
                            snap = _fault_snapshot_content(row["snapshot_json"])
                            snap["content"] = "corrupted-history-content"
                            dumped = json.dumps(snap, ensure_ascii=False, sort_keys=True)
                            connection.execute(
                                "UPDATE knowledge_object_versions SET snapshot_json=? WHERE id=?",
                                (dumped, row["id"]),
                            )
                            check = connection.execute(
                                "SELECT snapshot_json FROM knowledge_object_versions WHERE id=?",
                                (row["id"],),
                            ).fetchone()
                            if (
                                check is not None
                                and _fault_snapshot_content(check["snapshot_json"]).get("content")
                                == "corrupted-history-content"
                            ):
                                note()
        if method == "POST" and response.status_code == 200:
            single = url.endswith("/review") and not url.endswith("/bulk-review")
            bulk = url.endswith("/bulk-review")
            if single or bulk:
                body = response.json()
                items = [body["item"]] if "item" in body else body.get("changed") or []
                with storage.transaction() as connection:
                    for item in items:
                        item_id = item["id"]
                        if (fault == "review_dropped" and single) or (fault == "bulk_dropped" and bulk):
                            connection.execute(
                                "UPDATE knowledge_conflicts SET status='suggested' WHERE id=?", (item_id,)
                            )
                            if _fault_row(connection, "knowledge_conflicts", item_id, "status", "suggested"):
                                note()
                        elif fault == "review_row_collateral":
                            connection.execute(
                                "UPDATE knowledge_conflicts SET confidence=0.01 WHERE id=?", (item_id,)
                            )
                            if _fault_row(connection, "knowledge_conflicts", item_id, "confidence", 0.01):
                                note()
        return response

    monkeypatch.setattr(TestClient, "request", altered)
    scenario = {
        "stages": _assert_stages,
        "conflict": _assert_conflicts,
        "apply": _assert_apply,
        "review": _assert_review,
        "authority": _assert_authority,
    }[scenario_name]
    with pytest.raises(AssertionError, match=code):
        scenario(ctx)
    assert hits, f"lc_fault_not_injected:{fault}"
    assert all(item == fault for item in hits), f"lc_fault_wrong_injection:{fault}"
