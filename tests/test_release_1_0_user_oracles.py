"""User administration observes real HTTP, persisted accounts and subsequent auth."""

from __future__ import annotations

import json
from dataclasses import replace

import httpx
import pytest
from fastapi.testclient import TestClient

from friday.permissions import LEGACY_OWNER_USER_ID
from friday.server import create_app
from tests.test_api_tokens import _issue
from tests.test_release_1_0_conversation_oracles import _audit, _body, _equal

_A, _B, _NEW = "local:r10-user-a", "local:r10-user-b", "local:r10-user-new"
_ADMIN, _ORDINARY = "local:r10-user-admin", "local:r10-user-ordinary"


@pytest.fixture
def user_http(settings):
    assert not settings.llm_enabled and not settings.workers_enabled
    app = create_app(replace(settings, shared_archive=True, telegram_owner_chat_ids=[]))
    with TestClient(app) as client:
        ctx = {
            "client": client,
            "storage": app.state.storage,
            "owner": {"Authorization": f"Bearer {settings.api_token}"},
            "headers": {},
        }
        for key, person, preset in [
            ("a", _A, "user"),
            ("b", _B, "user"),
            ("admin", _ADMIN, "admin"),
            ("ordinary", _ORDINARY, "user"),
        ]:
            secret = "jrc_synthetic_user_oracle_" + key
            _issue(app.state.storage, person, preset, secret)
            ctx["headers"][key] = {"Authorization": f"Bearer {secret}"}
        yield ctx


def _rows(ctx):
    rows = [dict(row) for row in ctx["storage"].execute("SELECT * FROM users ORDER BY id").fetchall()]
    for row in rows:
        # Authentication may touch acting accounts. Preserve every authority,
        # identity and metadata field, including those of the acting accounts.
        if row["id"] in {LEGACY_OWNER_USER_ID, _ADMIN, _ORDINARY}:
            row.pop("updated_at")
            row.pop("last_seen_at")
    return rows


def _assert_fields(row, expected, code):
    assert row is not None, code + "_missing"
    _equal(
        {key: row[key] for key in expected if key != "metadata"},
        {key: value for key, value in expected.items() if key != "metadata"},
        code,
    )
    if "metadata" in expected:
        _equal(json.loads(row["metadata_json"]), expected["metadata"], code + "_metadata")


def _assert_audit(ctx, action, target, count):
    rows = [row for row in _audit(ctx["storage"], action) if row["target_id"] == target]
    _equal(len(rows), count, "user_audit_count")
    assert all(row["user_id"] == LEGACY_OWNER_USER_ID and row["target_type"] == "user" for row in rows), (
        "user_audit_actor"
    )


def _assert_create(ctx):
    before = _rows(ctx)
    expected = {
        "id": _NEW,
        "source": "local",
        "external_id": "synthetic-identity-17",
        "display_name": "Ирина 17",
        "username": "irina_17",
        "preset_key": "user",
        "metadata": {"label": "original", "preserve": 7},
    }
    body = _body(ctx["client"].post("/api/admin/users", headers=ctx["owner"], json=expected))
    _equal(set(body), {"user", "created_by"}, "user_create_shape")
    _equal(body["created_by"], LEGACY_OWNER_USER_ID, "user_create_actor")
    row = ctx["storage"].get_user(_NEW)
    _assert_fields(row, {**expected, "status": "active"}, "user_create_persisted")
    _equal(body["user"], row, "user_create_http_matches_storage")
    _equal([r for r in _rows(ctx) if r["id"] != _NEW], before, "user_create_preserves_others")
    _assert_audit(ctx, "admin.user.upsert", _NEW, 1)
    created_at = row["created_at"]
    updated = {
        **expected,
        "display_name": "Ирина обновлена",
        "preset_key": "admin",
        "metadata": {"label": "replacement"},
    }
    again = _body(ctx["client"].post("/api/admin/users", headers=ctx["owner"], json=updated))
    row = ctx["storage"].get_user(_NEW)
    _assert_fields(
        row,
        {
            **updated,
            "metadata": {"label": "replacement", "preserve": 7},
            "status": "active",
            "created_at": created_at,
        },
        "user_upsert_persisted",
    )
    _equal(again["user"], row, "user_upsert_http_matches_storage")
    _equal(len(_rows(ctx)), len(before) + 1, "user_upsert_one_account")
    _equal([r for r in _rows(ctx) if r["id"] != _NEW], before, "user_upsert_preserves_others")
    _assert_audit(ctx, "admin.user.upsert", _NEW, 2)


def test_user_create_and_repeat_upsert_have_exact_person_fields_preservation_and_audit(user_http):
    _assert_create(user_http)


def test_user_create_preserves_explicit_source_and_external_identity(user_http):
    ctx = user_http
    expected = {
        "id": _NEW,
        "source": "admin",
        "external_id": "synthetic-origin-23",
        "display_name": "Explicit origin",
        "preset_key": "user",
    }
    body = _body(ctx["client"].post("/api/admin/users", headers=ctx["owner"], json=expected))
    _assert_fields(ctx["storage"].get_user(_NEW), expected, "user_explicit_source_preserved")
    _equal(body["user"], ctx["storage"].get_user(_NEW), "user_source_http_matches_storage")


def _assert_patch(ctx):
    client, storage = ctx["client"], ctx["storage"]
    before = storage.get_user(_A)
    other_accounts = [row for row in _rows(ctx) if row["id"] != _A]
    _equal(
        _body(client.get("/api/me", headers=ctx["headers"]["a"]))["actor"]["preset_key"],
        "user",
        "user_patch_previously_user",
    )
    wanted = {
        "display_name": "Изменён только A",
        "username": "only_a",
        "status": "disabled",
        "preset_key": "admin",
        "metadata": {"note": "private A", "sequence": 11},
    }
    body = _body(client.patch(f"/api/admin/users/{_A}", headers=ctx["owner"], json=wanted))
    _equal(set(body), {"user"}, "user_patch_shape")
    row = storage.get_user(_A)
    _assert_fields(
        row,
        {**wanted, **{key: before[key] for key in ("id", "source", "external_id", "created_at")}},
        "user_patch_persisted",
    )
    _equal(body["user"], row, "user_patch_http_matches_storage")
    _equal([item for item in _rows(ctx) if item["id"] != _A], other_accounts, "user_patch_preserves_others")
    _assert_audit(ctx, "admin.user.update", _A, 1)
    assert client.get("/api/me", headers=ctx["headers"]["a"]).status_code == 401, "user_disabled_auth"
    _equal(
        _body(client.get("/api/me", headers=ctx["headers"]["b"]))["actor"]["user_id"],
        _B,
        "user_patch_sibling_auth",
    )
    body = _body(client.patch(f"/api/admin/users/{_A}", headers=ctx["owner"], json={"status": "active"}))
    _assert_fields(storage.get_user(_A), {**wanted, "status": "active"}, "user_reactivate_persisted")
    _equal(body["user"], storage.get_user(_A), "user_reactivate_http_matches_storage")
    _equal(
        _body(client.get("/api/me", headers=ctx["headers"]["a"]))["actor"]["preset_key"],
        "admin",
        "user_patch_new_preset_auth",
    )
    assert client.get("/api/admin/users", headers=ctx["headers"]["a"]).status_code == 200, (
        "user_patch_effective_admin_permission"
    )
    _assert_audit(ctx, "admin.user.update", _A, 2)


def test_user_patch_persists_exact_fields_and_disable_reactivate_changes_actual_auth(user_http):
    _assert_patch(user_http)


_BAD_REQUESTS = [
    ("POST", "/api/admin/users", {"id": "invalid id with spaces"}, 400),
    ("POST", "/api/admin/users", {"id": _NEW, "preset_key": "missing-preset"}, 400),
    ("PATCH", f"/api/admin/users/{_A}", {"status": "paused", "display_name": "must not store"}, 400),
    ("PATCH", f"/api/admin/users/{_A}", {"metadata": "not an object"}, 400),
    ("PATCH", f"/api/admin/users/{_A}", {"preset_key": "missing-preset"}, 400),
    ("PATCH", "/api/admin/users/local:r10-user-missing", {"status": "disabled"}, 404),
]


def _assert_invalid(ctx):
    before = _rows(ctx)
    audits = {action: _audit(ctx["storage"], action) for action in ("admin.user.upsert", "admin.user.update")}
    for method, url, body, status in _BAD_REQUESTS:
        response = ctx["client"].request(method, url, headers=ctx["owner"], json=body)
        _equal(response.status_code, status, "user_invalid_status")
        _equal(_rows(ctx), before, "user_invalid_no_effect")
        for action, rows in audits.items():
            _equal(_audit(ctx["storage"], action), rows, "user_invalid_no_success_audit")


def test_user_create_patch_invalid_inputs_and_missing_person_have_no_persisted_effect(user_http):
    _assert_invalid(user_http)


def test_user_administration_refuses_anonymous_ordinary_and_delegated_owner_mutation(user_http):
    ctx = user_http
    before = _rows(ctx)
    for headers, status in [({}, 401), (ctx["headers"]["ordinary"], 403)]:
        for method, url, body in [
            ("POST", "/api/admin/users", {"id": _NEW}),
            ("PATCH", f"/api/admin/users/{_A}", {"status": "disabled"}),
        ]:
            response = ctx["client"].request(method, url, headers=headers, json=body)
            _equal(response.status_code, status, "user_authority_refusal")
            _equal(_rows(ctx), before, "user_authority_no_effect")
    owner_before = ctx["storage"].get_user(LEGACY_OWNER_USER_ID)
    for method, url, body in [
        ("POST", "/api/admin/users", {"id": LEGACY_OWNER_USER_ID, "display_name": "takeover"}),
        ("POST", "/api/admin/users", {"id": _NEW, "preset_key": "owner"}),
        ("PATCH", f"/api/admin/users/{LEGACY_OWNER_USER_ID}", {"status": "disabled"}),
        ("PATCH", f"/api/admin/users/{_A}", {"preset_key": "owner"}),
    ]:
        response = ctx["client"].request(method, url, headers=ctx["headers"]["admin"], json=body)
        _equal(response.status_code, 403, "user_owner_protection")
        _equal(_rows(ctx), before, "user_owner_protection_no_effect")
        _equal(ctx["storage"].get_user(LEGACY_OWNER_USER_ID), owner_before, "user_owner_unchanged")


_FAULTS = [
    ("create_fields", "user_create_persisted"),
    ("create_response", "user_create_http_matches_storage"),
    ("create_collateral", "user_create_preserves_others"),
    ("create_audit", "user_audit_count"),
    ("patch_unwritten", "user_patch_persisted"),
    ("patch_collateral", "user_patch_preserves_others"),
    ("patch_preset", "user_patch_persisted"),
    ("patch_auth", "user_disabled_auth"),
    ("invalid_false_refusal", "user_invalid_no_effect"),
]


@pytest.mark.parametrize("fault,code", _FAULTS, ids=[row[0] for row in _FAULTS])
def test_user_oracles_detect_corrupted_http_and_actual_account_mutations(user_http, monkeypatch, fault, code):
    ctx, original = user_http, TestClient.request
    storage = ctx["storage"]
    if fault == "create_audit":
        original_audit = storage.log_audit

        def omit_create_audit(entry):
            if entry.action == "admin.user.upsert" and entry.target_id == _NEW:
                return None
            return original_audit(entry)

        monkeypatch.setattr(storage, "log_audit", omit_create_audit)

    def altered(self, method, url, **kwargs):
        method = method.upper()
        prior = storage.get_user(_A)
        false_refusal = (
            fault == "invalid_false_refusal"
            and method == "PATCH"
            and kwargs.get("json", {}).get("status") == "paused"
        )
        if false_refusal:
            kwargs["json"] = {**kwargs["json"], "status": "active"}
        response = original(self, method, url, **kwargs)
        if false_refusal:
            assert response.status_code == 200, "fault_setup_actual_write"
            return httpx.Response(400, json={"detail": "synthetic false refusal"}, request=response.request)
        if fault == "patch_auth" and method == "GET" and url == "/api/me" and response.status_code == 401:
            return httpx.Response(200, json={"actor": {"user_id": _A}}, request=response.request)
        target = _NEW if fault.startswith("create") else _A
        match = (method == "POST" and url == "/api/admin/users" and target == _NEW) or (
            method == "PATCH" and url == f"/api/admin/users/{_A}" and target == _A
        )
        if response.status_code != 200 or not match:
            return response
        if fault == "create_response":
            body = response.json()
            body["user"]["display_name"] = "invented outward name"
            return httpx.Response(200, json=body, request=response.request)
        with storage.transaction() as connection:
            if fault in {"create_fields", "patch_preset"}:
                connection.execute(
                    "UPDATE users SET preset_key=? WHERE id=?",
                    ("moderator" if target == _NEW else "user", target),
                )
            elif fault.endswith("collateral"):
                connection.execute("UPDATE users SET username='corrupted-sibling' WHERE id=?", (_B,))
            elif fault == "patch_unwritten":
                connection.execute(
                    "UPDATE users SET display_name=?,username=?,status=?,preset_key=?,metadata_json=? WHERE id=?",
                    (
                        *(
                            prior[key]
                            for key in ("display_name", "username", "status", "preset_key", "metadata_json")
                        ),
                        _A,
                    ),
                )
        return response

    monkeypatch.setattr(TestClient, "request", altered)
    scenario = {"create": _assert_create, "patch": _assert_patch, "invalid": _assert_invalid}[
        fault.split("_")[0]
    ]
    with pytest.raises(AssertionError, match=code):
        scenario(ctx)


def _assert_delete_refusals(settings):
    # Production hard-delete remains code-owned off. This fixture exercises the
    # authorization boundary inside its explicitly enabled test-only handler.
    assert not settings.llm_enabled and not settings.workers_enabled
    app = create_app(replace(settings, account_hard_delete_enabled=True, shared_archive=True))
    with TestClient(app) as client:
        storage = app.state.storage
        headers = {}
        for key, person, preset in [
            ("a", _A, "user"),
            ("b", _B, "user"),
            ("admin", _ADMIN, "admin"),
            ("ordinary", _ORDINARY, "user"),
        ]:
            secret = "jrc_synthetic_delete_refusal_" + key
            _issue(storage, person, preset, secret)
            headers[key] = {"Authorization": f"Bearer {secret}"}
        storage.update_user(_A, status="disabled")
        system = "local:r10-protected-system"
        storage.ensure_user(system, source="system")
        owner = {"Authorization": f"Bearer {settings.api_token}"}
        targets = (_A, _B, _ADMIN, LEGACY_OWNER_USER_ID, system)
        before = _rows({"storage": storage})
        from friday.storage import deleted_account_tombstone_key

        for auth, target, status in [
            ({}, _A, 401),
            (headers["ordinary"], _A, 403),
            (headers["admin"], LEGACY_OWNER_USER_ID, 403),
            (headers["admin"], _ADMIN, 409),
            (owner, LEGACY_OWNER_USER_ID, 409),
            (owner, system, 403),
        ]:
            for method, suffix in [("GET", "/deletion"), ("DELETE", "")]:
                response = client.request(
                    method,
                    f"/api/admin/users/{target}{suffix}",
                    headers=auth,
                    json={"confirmation": target, "fingerprint": "0" * 64},
                )
                _equal(response.status_code, status, "user_delete_authority_refusal")
                _equal(
                    _rows({"storage": storage}),
                    before,
                    "user_delete_refusal_no_effect",
                )
                assert all(
                    storage.kv_get(deleted_account_tombstone_key(person)) is None for person in targets
                ), "user_delete_refusal_no_tombstones"
        _equal(_audit(storage, "admin.user.delete"), [], "user_delete_refusal_no_success_audit")


def test_user_deletion_get_and_delete_enforce_actual_authority_without_account_effects(settings):
    _assert_delete_refusals(settings)


def test_user_delete_refusal_oracle_detects_a_real_write_after_the_http_refusal(settings, monkeypatch):
    original = TestClient.request

    def write_after_refusal(self, method, url, **kwargs):
        response = original(self, method, url, **kwargs)
        if method == "DELETE" and url == f"/api/admin/users/{_A}" and response.status_code == 403:
            self.app.state.storage.update_user(_A, display_name="mutated despite refusal")
        return response

    monkeypatch.setattr(TestClient, "request", write_after_refusal)
    with pytest.raises(AssertionError, match="user_delete_refusal_no_effect"):
        _assert_delete_refusals(settings)
