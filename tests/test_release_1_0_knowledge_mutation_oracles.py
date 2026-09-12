"""Knowledge edit, version restore and two-phase deletion through real HTTP."""

from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime

import httpx
import pytest

from friday.permissions import LEGACY_OWNER_USER_ID
from tests.test_organs_profile_chronicle import _seed_knowledge
from tests.test_release_1_0_conversation_oracles import _body, _equal
from tests.test_release_1_0_knowledge_read_oracles import _A, _ADMIN, _B, _audits, _read_state
from tests.test_release_1_0_knowledge_read_oracles import knowledge_read_http as knowledge_read_http
from tests.test_release_1_0_profile_oracles import profile_http as profile_http

_KINDS = ("edit", "restore", "delete", "purge", "purgeable")
_EDIT = {
    "title": "Изменённый архив A1",
    "summary": "Сводка правки A1",
    "content": "PRIVATE_EDIT_КАЛИБРОВКА_17",
    "tags_json": ["calibration", "reviewed"],
    "metadata_json": {"document_date": "2024-05-19", "operator_note": "PRIVATE_NOTE_17"},
    "importance": 0.8,
    "lifecycle_stage": "archived",
    "knowledge_kind": "document",
    "quality_score": 0.7,
    "promotion_score": 0.6,
}
_EXTRA_TABLES = (
    "knowledge_embeddings",
    "knowledge_chunk_embeddings",
    "knowledge_usage",
    "knowledge_conflicts",
    "feedback",
    "feedback_state",
    "file_source_aliases",
    "document_catalog",
    "document_passages",
    "document_passage_projections",
)


@pytest.fixture
def mutation_http(knowledge_read_http):
    ctx = knowledge_read_http
    ctx["owner_knowledge"] = _seed_knowledge(
        ctx["storage"], LEGACY_OWNER_USER_ID, "OWNER_PRIVATE_КАЛИБРОВКА", []
    )
    yield ctx


def _state(ctx):
    result = _read_state(ctx)
    for table in _EXTRA_TABLES:
        result[table] = [
            dict(r) for r in ctx["storage"].execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
        ]
    return result


def _row(ctx):
    return ctx["storage"].get_knowledge_object(ctx["first"], _A)


def _history(ctx):
    return {
        r["version"]: {**r, "snapshot_json": json.loads(r["snapshot_json"])}
        for r in ctx["storage"].list_knowledge_versions(ctx["first"], _A)
    }


def _request(ctx, kind, *, headers=None, target=_A, key=None, payload=None):
    key = ctx["first"] if key is None else key
    path = "/api/admin/knowledge/" + key
    headers = ctx["admin"] if headers is None else headers
    if kind == "purgeable":
        return ctx["client"].get(
            "/api/admin/data/purgeable",
            headers=headers,
            params={"user_id": target, "older_than_days": 0, "limit": 1},
        )
    if kind == "delete":
        return ctx["client"].delete(path, headers=headers, params={"user_id": target})
    if kind == "purge":
        return ctx["client"].post(path + "/purge", headers=headers, params={"user_id": target}, json={})
    body = {**(_EDIT if kind == "edit" else {"version": 1}), **(payload or {}), "user_id": target}
    if kind == "edit":
        return ctx["client"].patch(path, headers=headers, json=body)
    return ctx["client"].post(path + "/restore", headers=headers, json=body)


def _prepare(ctx, kind):
    storage = ctx["storage"]
    if kind == "restore":
        storage.update_knowledge_fields(ctx["first"], _A, lifecycle_stage="archived", quality_score=0.7)
    if kind in {"purge", "purgeable"}:
        assert storage.soft_delete_knowledge_object(ctx["first"], _A)
    if kind == "purgeable":
        assert storage.soft_delete_knowledge_object(ctx["foreign"], _B)


def _timestamp(value, started, code):
    stamp = datetime.fromisoformat(value)
    assert stamp.tzinfo is not None and started <= stamp <= datetime.now(UTC), code


def _fingerprint(row):
    return {
        "id": row["id"],
        "title_chars": len(row["title"]),
        "knowledge_kind": row["knowledge_kind"],
        "lifecycle_stage": row["lifecycle_stage"],
        "version": row["version"],
        "content_chars": len(row["content"]),
        "content_sha256": hashlib.sha256(row["content"].encode()).hexdigest(),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _assert_audit(ctx, prior, captured, specs):
    rows = _audits(ctx)
    _equal(rows[: len(prior)], prior, "knowledge_mutation_audit_history")
    _equal(
        (len(rows) - len(prior), len(captured)), (len(specs), len(specs)), "knowledge_mutation_audit_count"
    )
    for row, raw, (action, before, after) in zip(rows[len(prior) :], captured, specs, strict=True):
        _equal(
            (raw.user_id, raw.action, raw.target_type, raw.target_id),
            (_ADMIN, action, "knowledge_object", ctx["first"]),
            "knowledge_mutation_raw_actor",
        )
        _equal((raw.before_json, raw.after_json), (before, after), "knowledge_mutation_raw_fingerprint")
        _equal(
            (row["user_id"], row["action"], row["target_type"], row["target_id"]),
            (_ADMIN, action, "knowledge_object", ctx["first"]),
            "knowledge_mutation_audit_actor",
        )
        for field, expected in (("before_json", before), ("after_json", after)):
            wanted = deepcopy(expected)
            actual = json.loads(row[field]) if row[field] else None
            if action == "admin.knowledge.purge" and field == "after_json":
                # Closed audit schema hides all seven public receipt fields.
                # Raw audit and HTTP receipt are checked independently.
                wanted = {"private_fields_count": 7, "private_chars": 64}
            if isinstance(wanted, dict) and "restored_from_version" in wanted:
                wanted.pop("restored_from_version")
                wanted["private_fields_count"] = 1
            if isinstance(wanted, dict) and "content_sha256" in wanted:
                wanted.pop("content_sha256")
                assert re.fullmatch(r"fpref_[0-9a-f]+", actual.get("content_ref", "")), (
                    "knowledge_mutation_keyed_fingerprint"
                )
                actual = {k: v for k, v in actual.items() if k != "content_ref"}
            _equal(actual, wanted, "knowledge_mutation_audit_projection")
        blob = json.dumps(row, ensure_ascii=False)
        for canary in (
            "Иванов",
            "Архив A1",
            "PRIVATE_EDIT_КАЛИБРОВКА_17",
            "PRIVATE_NOTE_17",
            "FOREIGN_PRIVATE",
            "OWNER_PRIVATE",
        ):
            assert canary not in blob, "knowledge_mutation_audit_private_content"


def _assert_mutation(ctx, monkeypatch, kind):
    _prepare(ctx, kind)
    storage = ctx["storage"]
    before, prior, old = _state(ctx), _audits(ctx), _row(ctx)
    history = _history(ctx)
    captured = []
    original = storage.log_audit

    def capture(entry):
        captured.append(deepcopy(entry))
        return original(entry)

    monkeypatch.setattr(storage, "log_audit", capture)
    started = datetime.now(UTC).replace(microsecond=0)
    response = _request(ctx, kind)
    body = _body(response)
    after = _row(ctx)
    if kind == "purgeable":
        _equal(
            (body["count"], body["older_than_days"], [r["id"] for r in body["items"]]),
            (1, 0, [ctx["first"]]),
            "knowledge_purgeable_membership",
        )
        _equal(
            body["items"],
            [{"id": ctx["first"], "user_id": _A, "title": "Архив A1", "deleted_at": old["deleted_at"]}],
            "knowledge_purgeable_item_fields",
        )
        _equal(_state(ctx), before, "knowledge_purgeable_no_write")
        rows = _audits(ctx)
        _equal(rows[: len(prior)], prior, "knowledge_purgeable_audit_history")
        _equal(len(rows) - len(prior), 1, "knowledge_purgeable_audit_count")
        _equal(
            (rows[-1]["user_id"], rows[-1]["action"], rows[-1]["target_id"]),
            (_ADMIN, "admin.purge.read", _A),
            "knowledge_purgeable_audit_actor",
        )
        assert "Иванов" not in json.dumps(rows[-1], ensure_ascii=False)
        return
    expected_state = deepcopy(before)
    if kind == "purge":
        _equal(after, None, "knowledge_purge_removed")
        for table, column, value in (
            ("knowledge_objects", "id", ctx["first"]),
            ("knowledge_object_versions", "knowledge_object_id", ctx["first"]),
            ("knowledge_entity_links", "knowledge_object_id", ctx["first"]),
            ("raw_objects", "id", old["raw_object_id"]),
        ):
            expected_state[table] = [r for r in expected_state[table] if r[column] != value]
        _equal(_state(ctx), expected_state, "knowledge_purge_exact_rows")
        receipt = {
            "knowledge_object_ref_sha256": hashlib.sha256(ctx["first"].encode()).hexdigest(),
            "existed": True,
            "deleted_row_count": 7,
            "raw_removed": True,
            "file_unlinked": False,
            "vault_removed": False,
            "vault_removed_count": 0,
        }
        _equal(body, {"status": "purged", "report": receipt}, "knowledge_purge_receipt")
        _assert_audit(
            ctx,
            prior,
            captured,
            [
                ("admin.knowledge.purge_attempted", _fingerprint(old), {"status": "started"}),
                ("admin.knowledge.purge", _fingerprint(old), receipt),
            ],
        )
        repeat_before, repeat_audit = _state(ctx), _audits(ctx)
        _equal(_request(ctx, kind).status_code, 404, "knowledge_purge_repeat_missing")
        _equal((_state(ctx), _audits(ctx)), (repeat_before, repeat_audit), "knowledge_purge_repeat_no_effect")
        return
    assert after is not None, "knowledge_mutation_target_present"
    wanted = deepcopy(old)
    if kind == "edit":
        wanted.update(
            {
                k: json.dumps(v, ensure_ascii=False) if k in {"tags_json", "metadata_json"} else v
                for k, v in _EDIT.items()
            }
        )
    elif kind == "restore":
        wanted.update(
            title="Иванов и документ A1",
            summary="Иванов и документ A1",
            content="Иванов и документ A1",
            tags_json='["alpha"]',
            metadata_json=json.dumps({"restored_from_version": 1, "restored_by": _ADMIN}, ensure_ascii=False),
        )
    else:
        assert after["deleted_at"], "knowledge_delete_persisted"
        _timestamp(after["deleted_at"], started, "knowledge_delete_timestamp")
        wanted.update(lifecycle_stage="deleted", deleted_at=after["deleted_at"])
    wanted.update(version=old["version"] + 1, updated_at=after["updated_at"])

    # Compare JSON semantically; serialisation whitespace is not a product outcome.
    def decoded(row):
        return {k: json.loads(v) if k in {"tags_json", "metadata_json"} else v for k, v in row.items()}

    _equal(decoded(after), decoded(wanted), "knowledge_mutation_target")
    _timestamp(after["updated_at"], started, "knowledge_mutation_timestamp")
    _equal(
        body,
        {"status": "soft_deleted"}
        if kind == "delete"
        else {"item": after, **({"restored_from_version": 1} if kind == "restore" else {})},
        "knowledge_mutation_http",
    )
    for table, column in (("knowledge_objects", "id"), ("knowledge_object_versions", "knowledge_object_id")):
        expected_state[table] = [r for r in expected_state[table] if r[column] != ctx["first"]]
    remaining = _state(ctx)
    for table, column in (("knowledge_objects", "id"), ("knowledge_object_versions", "knowledge_object_id")):
        remaining[table] = [r for r in remaining[table] if r[column] != ctx["first"]]
    _equal(remaining, expected_state, "knowledge_mutation_other_rows")
    versions = _history(ctx)
    _equal(
        {v: r for v, r in versions.items() if v in history}, history, "knowledge_mutation_history_preserved"
    )
    _equal(sorted(versions), list(range(1, old["version"] + 2)), "knowledge_mutation_history_append")
    _equal(
        decoded(versions[after["version"]]["snapshot_json"]),
        decoded(after),
        "knowledge_mutation_history_snapshot",
    )
    extra = (
        {"changed_fields": sorted(_EDIT)}
        if kind == "edit"
        else {"restored_from_version": 1}
        if kind == "restore"
        else {}
    )
    action = {"edit": "update", "restore": "restore", "delete": "delete"}[kind]
    _assert_audit(
        ctx,
        prior,
        captured,
        [("admin.knowledge." + action, _fingerprint(old), {**extra, **_fingerprint(after)})],
    )
    if kind == "delete":
        refused_before, audit_before = _state(ctx), _audits(ctx)
        _equal(_request(ctx, kind).status_code, 404, "knowledge_delete_repeat_missing")
        _equal(
            (_state(ctx), _audits(ctx)), (refused_before, audit_before), "knowledge_delete_repeat_no_effect"
        )
        result = _body(
            ctx["client"].get(
                "/api/admin/knowledge", headers=ctx["admin"], params={"user_id": _A, "q": "Архив A1"}
            )
        )
        _equal((result["total"], result["items"]), (0, []), "knowledge_deleted_hidden_from_list")


@pytest.mark.parametrize("kind", _KINDS)
def test_knowledge_mutations_persist_exact_outcomes_history_and_personal_audit(
    mutation_http, monkeypatch, kind
):
    _assert_mutation(mutation_http, monkeypatch, kind)


def _assert_refusals(ctx, kind):
    for headers, status in (({}, 401), (ctx[_A], 403)):
        before, audits = _state(ctx), _audits(ctx)
        _equal(
            _request(ctx, kind, headers=headers).status_code, status, "knowledge_mutation_authority_refusal"
        )
        _equal(_state(ctx), before, "knowledge_mutation_refusal_no_effect")
        observed = _audits(ctx)
        _equal(observed[: len(audits)], audits, "knowledge_mutation_refusal_audit_history")
        added = observed[len(audits) :]
        _equal(len(added), 1 if status == 401 else 0, "knowledge_mutation_refusal_security_audit_count")
        if added:
            row = added[0]
            method = {
                "edit": "PATCH",
                "restore": "POST",
                "delete": "DELETE",
                "purge": "POST",
                "purgeable": "GET",
            }[kind]
            path = (
                "/api/admin/data/purgeable"
                if kind == "purgeable"
                else "/api/admin/knowledge/"
                + ctx["first"]
                + ("/" + kind if kind in {"restore", "purge"} else "")
            )
            _equal(
                (row["user_id"], row["action"], row["target_type"], row["target_id"]),
                ("anonymous", "auth.failed", "auth", "invalid_credentials"),
                "knowledge_mutation_refusal_security_audit",
            )
            _equal(
                json.loads(row["after_json"]),
                {
                    "reason": "invalid_credentials",
                    "status_present": True,
                    "method_chars": len(method),
                    "path_chars": len(path),
                },
                "knowledge_mutation_refusal_security_metadata",
            )
    cap = "admin.data.purge" if kind in {"purge", "purgeable"} else "admin.all_data.manage"
    ctx["storage"].set_permission_override(_ADMIN, cap, "deny")
    before, audits = _state(ctx), _audits(ctx)
    _equal(_request(ctx, kind).status_code, 403, "knowledge_mutation_explicit_deny")
    _equal((_state(ctx), _audits(ctx)), (before, audits), "knowledge_mutation_refusal_no_effect")


@pytest.mark.parametrize("kind", _KINDS)
def test_knowledge_mutations_refuse_anonymous_ordinary_and_explicit_denial_without_effect(
    mutation_http, kind
):
    _assert_refusals(mutation_http, kind)


@pytest.mark.parametrize("kind", ("edit", "restore", "delete", "purge"))
def test_knowledge_mutations_protect_owner_and_refuse_wrong_target_without_effect(mutation_http, kind):
    ctx = mutation_http
    for target, key, status in (
        (LEGACY_OWNER_USER_ID, ctx["owner_knowledge"], 403),
        (_B, ctx["first"], 404),
        (_A, "ko_missing_038", 404),
    ):
        before, audits = _state(ctx), _audits(ctx)
        _equal(
            _request(ctx, kind, target=target, key=key).status_code,
            status,
            "knowledge_mutation_target_refusal",
        )
        _equal((_state(ctx), _audits(ctx)), (before, audits), "knowledge_mutation_target_refusal_no_effect")


@pytest.mark.parametrize("kind", ("edit", "restore", "purge"))
def test_knowledge_mutation_invalid_input_and_active_purge_cannot_change_business_rows(mutation_http, kind):
    ctx = mutation_http
    payload = {"lifecycle_stage": "not-a-stage"} if kind == "edit" else {"version": "not-a-version"}
    before, audits = _state(ctx), _audits(ctx)
    response = _request(ctx, kind, payload=payload)
    _equal(response.status_code, 409 if kind == "purge" else 400, "knowledge_mutation_invalid_refused")
    _equal(_state(ctx), before, "knowledge_mutation_invalid_no_effect")
    after = _audits(ctx)
    _equal(after[: len(audits)], audits, "knowledge_mutation_invalid_audit_history")
    _equal(len(after) - len(audits), 1 if kind == "purge" else 0, "knowledge_mutation_invalid_audit_count")
    if kind == "purge":
        _equal(
            (after[-1]["user_id"], after[-1]["action"]),
            (_ADMIN, "admin.knowledge.purge_attempted"),
            "knowledge_active_purge_intent_only",
        )


_FAULTS = (
    ("edit_unwritten", "edit", "knowledge_mutation_target"),
    ("restore_wrong_version", "restore", "knowledge_mutation_target"),
    ("delete_unwritten", "delete", "knowledge_delete_persisted"),
    ("purge_unwritten", "purge", "knowledge_purge_removed"),
    ("purge_count", "purge", "knowledge_purge_receipt"),
    ("purgeable_foreign", "purgeable", "knowledge_purgeable_membership"),
    ("edit_collateral", "edit", "knowledge_mutation_other_rows"),
    ("refusal_write", "edit", "knowledge_mutation_refusal_no_effect"),
    ("audit_missing", "edit", "knowledge_mutation_audit_count"),
    ("audit_actor", "edit", "knowledge_mutation_audit_actor"),
    ("edit_response", "edit", "knowledge_mutation_http"),
    ("history_damage", "edit", "knowledge_mutation_history_preserved"),
    ("purge_repeat_collateral", "purge", "knowledge_purge_repeat_no_effect"),
)


@pytest.mark.parametrize("fault,kind,code", _FAULTS, ids=[f[0] for f in _FAULTS])
def test_knowledge_mutation_oracles_catch_actual_output_writer_history_and_collateral_faults(
    mutation_http, monkeypatch, fault, kind, code
):
    ctx = mutation_http
    storage = ctx["storage"]
    if fault == "edit_unwritten":
        monkeypatch.setattr(storage, "update_knowledge_fields", lambda *_args, **_kwargs: _row(ctx))
    elif fault == "restore_wrong_version":
        original = storage.restore_knowledge_version
        monkeypatch.setattr(
            storage,
            "restore_knowledge_version",
            lambda key, user, _version, **kw: original(key, user, 2, **kw),
        )
    elif fault == "delete_unwritten":
        monkeypatch.setattr(storage, "soft_delete_knowledge_object", lambda *_args, **_kwargs: True)
    elif fault == "purge_unwritten":
        import friday.admin_api._knowledge as api

        monkeypatch.setattr(
            api,
            "purge_knowledge",
            lambda *_args, **_kwargs: {
                "existed": True,
                "deleted": {"knowledge_objects": 7},
                "raw_removed": True,
            },
        )
    elif fault == "audit_missing":
        import friday.admin_api._knowledge as api

        monkeypatch.setattr(api, "_audit", lambda *_args, **_kwargs: None)
    elif fault == "audit_actor":
        original = storage.log_audit
        monkeypatch.setattr(storage, "log_audit", lambda entry: original(replace(entry, user_id=_B)))
    else:
        original = ctx["client"].request
        injected = False

        def damaged_request(method, url, **kwargs):
            nonlocal injected
            response = original(method, url, **kwargs)
            if fault == "refusal_write" and response.status_code == 403:
                injected = True
                with storage.transaction() as conn:
                    conn.execute("UPDATE users SET preset_key='admin' WHERE id=?", (_B,))
            if fault == "purge_repeat_collateral" and response.status_code == 404:
                injected = True
                with storage.transaction() as conn:
                    conn.execute("UPDATE users SET preset_key='admin' WHERE id=?", (_B,))
            if response.status_code == 200:
                if fault != "purge_repeat_collateral":
                    injected = True
                if fault in {"purge_count", "purgeable_foreign", "edit_response"}:
                    body = response.json()
                    if fault == "purge_count":
                        body["report"]["deleted_row_count"] += 1
                    elif fault == "purgeable_foreign":
                        body["items"].append(storage.get_knowledge_object(ctx["foreign"], _B))
                    else:
                        body["item"]["content"] = "CORRUPTED_HTTP_ONLY"
                    return httpx.Response(200, json=body, request=response.request)
                if fault == "edit_collateral":
                    with storage.transaction() as conn:
                        conn.execute(
                            "UPDATE knowledge_objects SET content='DAMAGED_NEIGHBOR' WHERE id=?",
                            (ctx["foreign"],),
                        )
                if fault == "history_damage":
                    with storage.transaction() as conn:
                        row = conn.execute(
                            "SELECT snapshot_json FROM knowledge_object_versions WHERE knowledge_object_id=? AND version=1",
                            (ctx["first"],),
                        ).fetchone()
                        snapshot = json.loads(row[0])
                        snapshot["content"] = "DAMAGED_HISTORY"
                        conn.execute(
                            "UPDATE knowledge_object_versions SET snapshot_json=? WHERE knowledge_object_id=? AND version=1",
                            (json.dumps(snapshot), ctx["first"]),
                        )
            return response

        monkeypatch.setattr(ctx["client"], "request", damaged_request)
    with pytest.raises(AssertionError, match=code):
        if fault == "refusal_write":
            _assert_refusals(ctx, kind)
        else:
            _assert_mutation(ctx, monkeypatch, kind)

    if fault in {
        "purge_repeat_collateral",
        "purge_count",
        "purgeable_foreign",
        "edit_collateral",
        "refusal_write",
        "edit_response",
        "history_damage",
    }:
        assert injected, "knowledge_mutation_fault_exercised"
