"""Administrative token HTTP, real auth and persisted effects; no live secrets."""

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
from friday.storage.models import AuditEntry
from tests.test_api_tokens import _issue
from tests.test_release_1_0_conversation_oracles import _audit, _body, _equal

_A, _B, _ADMIN = "r10-token-a", "r10-token-b", "r10-token-admin"
_META = {"id", "user_id", "label", "created_by", "created_at", "last_used_at", "revoked_at", "expires_at"}


def _rows(ctx):
    return [dict(row) for row in ctx["storage"].execute("SELECT * FROM api_tokens ORDER BY id").fetchall()]


def _without_auth_touch(rows, token_id):
    return [
        {key: value for key, value in row.items() if not (row["id"] == token_id and key == "last_used_at")}
        for row in rows
    ]


def _assert_only_auth_touch(ctx, before, token_id, started, code):
    after = _rows(ctx)
    _equal(_without_auth_touch(after, token_id), _without_auth_touch(before, token_id), code)
    row = next(item for item in after if item["id"] == token_id)
    assert row["last_used_at"] is not None, "token_authenticated_touch_present"
    observed = datetime.fromisoformat(row["last_used_at"])
    # Storage utc_now has second resolution; bound only the authenticated row.
    assert started.replace(microsecond=0) <= observed <= datetime.now(UTC).replace(microsecond=0), (
        "token_authenticated_touch_in_request_window"
    )
    return after


@pytest.fixture
def token_http(settings):
    assert not settings.llm_enabled and not settings.workers_enabled
    app = create_app(replace(settings, shared_archive=True, telegram_owner_chat_ids=[]))
    with TestClient(app) as client:
        ctx = {
            "client": client,
            "storage": app.state.storage,
            "owner": {"Authorization": f"Bearer {settings.api_token}"},
        }
        ctx["records"], ctx["secrets"] = {}, {}
        for index, (key, person, preset) in enumerate(
            [
                ("a", _A, "user"),
                ("sibling", _A, "user"),
                ("b", _B, "moderator"),
                ("admin", _ADMIN, "admin"),
                ("owner", LEGACY_OWNER_USER_ID, "owner"),
            ]
        ):
            secret = f"jrc_r10_synthetic_token_{key}"
            row = _issue(app.state.storage, person, preset, secret)
            with app.state.storage.transaction() as connection:
                connection.execute(
                    "UPDATE api_tokens SET created_at=? WHERE id=?",
                    (f"2024-01-01T00:00:0{index}+00:00", row["id"]),
                )
            ctx["records"][key] = row["id"]
            ctx["secrets"][key] = secret
        yield ctx


def _assert_listing(ctx, *, delegated=False):
    client, storage = ctx["client"], ctx["storage"]
    storage.revoke_api_token(ctx["records"]["sibling"])
    headers = {"Authorization": "Bearer " + ctx["secrets"]["admin"]} if delegated else ctx["owner"]
    for person in (None, _A, _B, _ADMIN, "missing-user"):
        for revoked in (False, True):
            params = {"include_revoked": str(revoked).lower()}
            if person is not None:
                params["user_id"] = person
            before = _rows(ctx)
            started = datetime.now(UTC)
            body = _body(client.get("/api/admin/tokens", params=params, headers=headers))
            expected_rows = before
            if delegated:
                # Derive every expected field from the pre-request snapshot.
                # Only the actual authenticated credential's bounded touch is
                # read after auth, so collateral writes cannot become expected.
                after = _assert_only_auth_touch(
                    ctx, before, ctx["records"]["admin"], started, "token_delegated_list_preserves_other_rows"
                )
                touch = next(row["last_used_at"] for row in after if row["id"] == ctx["records"]["admin"])
                expected_rows = [
                    {**row, "last_used_at": touch} if row["id"] == ctx["records"]["admin"] else row
                    for row in before
                ]
            else:
                _equal(_rows(ctx), before, "token_owner_list_preserves_all_rows")
            expected = [{key: row[key] for key in _META} for row in expected_rows]
            expected.sort(key=lambda row: row["created_at"], reverse=True)
            wanted = [
                row
                for row in expected
                if (person is None or row["user_id"] == person) and (revoked or row["revoked_at"] is None)
            ]
            _equal(set(body), {"items", "count"}, "token_list_shape")
            _equal(body["count"], len(wanted), "token_list_count")
            _equal(body["items"], wanted, "token_list_exact_metadata")
            assert not any(secret in json.dumps(body) for secret in ctx["secrets"].values()), (
                "token_list_secret"
            )


def test_token_listing_has_exact_filters_order_revocation_and_safe_metadata(token_http):
    _assert_listing(token_http)


def test_delegated_admin_token_listing_has_exact_safe_metadata_and_only_its_auth_touch(token_http):
    _assert_listing(token_http, delegated=True)


def _assert_mint(ctx):
    client, storage = ctx["client"], ctx["storage"]
    before = _rows(ctx)
    response = _body(
        client.post(
            "/api/admin/tokens",
            json={"user_id": _A, "label": "acceptance-phone", "ttl_seconds": 3600},
            headers=ctx["owner"],
        )
    )
    _equal(set(response), {"token", "id", "user_id", "label", "expires_at"}, "token_mint_shape")
    raw = storage.execute("SELECT * FROM api_tokens WHERE id=?", (response["id"],)).fetchone()
    assert raw is not None, "token_mint_persisted"
    row = dict(raw)
    _equal(
        (response["user_id"], row["user_id"], response["label"], row["label"], row["created_by"]),
        (_A, _A, "acceptance-phone", "acceptance-phone", LEGACY_OWNER_USER_ID),
        "token_mint_identity",
    )
    secret = response["token"]
    assert isinstance(secret, str) and secret.startswith("jrc_") and secret not in ctx["secrets"].values(), (
        "token_mint_secret"
    )
    _equal(row["token_sha256"], hashlib.sha256(secret.encode()).hexdigest(), "token_mint_secret_hash")
    assert response["expires_at"] is not None and row["expires_at"] == response["expires_at"], (
        "token_mint_expiry"
    )
    _equal(
        (
            datetime.fromisoformat(row["expires_at"]) - datetime.fromisoformat(row["created_at"])
        ).total_seconds(),
        3600,
        "token_mint_exact_ttl",
    )
    after = _rows(ctx)
    _equal([item for item in after if item["id"] != response["id"]], before, "token_mint_preserves_siblings")
    assert len(after) == len(before) + 1 and secret not in json.dumps(after), "token_mint_only_hash_stored"
    audit = [
        dict(item)
        for item in storage.execute(
            "SELECT * FROM audit_log WHERE action='admin.token.create' AND target_id=?", (response["id"],)
        ).fetchall()
    ]
    assert (
        len(audit) == 1
        and audit[0]["user_id"] == LEGACY_OWNER_USER_ID
        and audit[0]["target_type"] == "api_token"
    ), "token_mint_audit"
    assert secret not in json.dumps(audit) and row["token_sha256"] not in json.dumps(audit), (
        "token_mint_audit_secret"
    )
    before_auth, started = _rows(ctx), datetime.now(UTC)
    me = _body(client.get("/api/me", headers={"Authorization": f"Bearer {secret}"}))
    _assert_only_auth_touch(ctx, before_auth, response["id"], started, "token_mint_auth_preserves_siblings")
    _equal(
        (me["actor"]["user_id"], me["actor"]["preset_key"]),
        (_A, "user"),
        "token_mint_authenticates_bound_person",
    )


def test_token_mint_persists_one_hash_exact_ttl_audit_and_authenticates_bound_person(token_http):
    _assert_mint(token_http)


def _assert_revoke(ctx):
    client, storage, target = ctx["client"], ctx["storage"], ctx["records"]["a"]
    before_auth_rows, started = _rows(ctx), datetime.now(UTC)
    before_auth = _body(client.get("/api/me", headers={"Authorization": "Bearer " + ctx["secrets"]["a"]}))
    _equal(before_auth["actor"]["user_id"], _A, "token_revoke_previously_authenticated")
    _assert_only_auth_touch(
        ctx, before_auth_rows, target, started, "token_revoke_pre_auth_preserves_other_rows"
    )
    before = _rows(ctx)
    audit_before = _audit(storage, "admin.token.revoke")
    _equal(
        _body(client.delete(f"/api/admin/tokens/{target}", headers=ctx["owner"])),
        {"status": "revoked"},
        "token_revoke_http",
    )
    row = next((item for item in _rows(ctx) if item["id"] == target), None)
    assert row is not None and row["revoked_at"] is not None, "token_revoke_persisted"
    previous = next(item for item in before if item["id"] == target)
    _equal(
        {key: value for key, value in row.items() if key != "revoked_at"},
        {key: value for key, value in previous.items() if key != "revoked_at"},
        "token_revoke_preserves_target_fields",
    )
    _equal(
        [row for row in _rows(ctx) if row["id"] != target],
        [row for row in before if row["id"] != target],
        "token_revoke_preserves_siblings",
    )
    audit_after = _audit(storage, "admin.token.revoke")
    _equal(audit_after[: len(audit_before)], audit_before, "token_revoke_preserves_prior_audits")
    added = audit_after[len(audit_before) :]
    assert (
        len(added) == 1
        and added[0]["user_id"] == LEGACY_OWNER_USER_ID
        and added[0]["target_id"] == target
        and added[0]["target_type"] == "api_token"
    ), "token_revoke_audit"
    after_delete = _rows(ctx)
    assert (
        client.get("/api/me", headers={"Authorization": "Bearer " + ctx["secrets"]["a"]}).status_code == 401
    ), "token_revoke_auth_denied"
    _equal(_rows(ctx), after_delete, "token_revoked_auth_has_no_touch")
    for key, person in (("sibling", _A), ("b", _B)):
        before_auth_rows, started = _rows(ctx), datetime.now(UTC)
        me = _body(client.get("/api/me", headers={"Authorization": "Bearer " + ctx["secrets"][key]}))
        _equal(me["actor"]["user_id"], person, "token_revoke_sibling_auth")
        _assert_only_auth_touch(
            ctx, before_auth_rows, ctx["records"][key], started, "token_sibling_auth_preserves_other_rows"
        )
    stable = _rows(ctx)
    for token_id in (target, "missing-token"):
        assert client.delete(f"/api/admin/tokens/{token_id}", headers=ctx["owner"]).status_code == 404
        _equal(_rows(ctx), stable, "token_revoke_repeat_no_effect")
        _equal(_audit(storage, "admin.token.revoke"), audit_after, "token_revoke_no_extra_audit")


def test_token_revocation_preserves_sibling_auth_and_repeat_cannot_mutate_again(token_http):
    _assert_revoke(token_http)


def test_token_management_refuses_anonymous_ordinary_and_owner_escalation_without_target_changes(token_http):
    ctx, client = token_http, token_http["client"]
    for headers, status, auth_key in (
        ({}, 401, None),
        ({"Authorization": "Bearer " + ctx["secrets"]["a"]}, 403, "a"),
    ):
        for method, url, body in (
            ("GET", "/api/admin/tokens", None),
            ("POST", "/api/admin/tokens", {"user_id": _A}),
            ("DELETE", "/api/admin/tokens/" + ctx["records"]["b"], None),
        ):
            before, started = _rows(ctx), datetime.now(UTC)
            assert client.request(method, url, json=body, headers=headers).status_code == status
            if auth_key is None:
                _equal(_rows(ctx), before, "token_refusal_no_target_changes")
            else:
                _assert_only_auth_touch(
                    ctx, before, ctx["records"][auth_key], started, "token_refusal_no_target_changes"
                )
    admin = {"Authorization": "Bearer " + ctx["secrets"]["admin"]}
    for method, url, body in (
        ("POST", "/api/admin/tokens", {"user_id": LEGACY_OWNER_USER_ID}),
        ("DELETE", "/api/admin/tokens/" + ctx["records"]["owner"], None),
    ):
        before, started = _rows(ctx), datetime.now(UTC)
        assert client.request(method, url, json=body, headers=admin).status_code == 403
        _assert_only_auth_touch(
            ctx, before, ctx["records"]["admin"], started, "token_admin_owner_refusal_no_effect"
        )


def test_token_mint_refuses_fractional_ttl_without_persisting_a_credential(token_http):
    ctx = token_http
    before = _rows(ctx)
    response = ctx["client"].post(
        "/api/admin/tokens", json={"user_id": _A, "ttl_seconds": 1.5}, headers=ctx["owner"]
    )
    assert response.status_code == 400, "token_fractional_ttl_refused"
    _equal(_rows(ctx), before, "token_fractional_ttl_no_effect")


_FAULTS = (
    ("list_filter", "token_list_count"),
    ("list_hash", "token_list_exact_metadata"),
    ("mint_missing", "token_mint_persisted"),
    ("mint_foreign", "token_mint_identity"),
    ("mint_secret", "token_mint_secret_hash"),
    ("mint_ttl", "token_mint_expiry"),
    ("revoke_unwritten", "token_revoke_persisted"),
    ("revoke_foreign", "token_revoke_preserves_siblings"),
    ("ttl_false_refusal", "token_fractional_ttl_no_effect"),
)


@pytest.mark.parametrize("fault,code", _FAULTS, ids=[row[0] for row in _FAULTS])
def test_token_oracles_detect_corrupt_http_and_real_persistence_effects(token_http, monkeypatch, fault, code):
    ctx, storage = token_http, token_http["storage"]
    if fault.startswith("list"):
        original = storage.list_api_tokens

        def bad_list(user_id=None, **kwargs):
            rows = original(None if fault == "list_filter" else user_id, **kwargs)
            if fault == "list_hash" and rows:
                rows[0]["token_sha256"] = "synthetic-hash-leak"
            return rows

        monkeypatch.setattr(storage, "list_api_tokens", bad_list)
    elif fault in {"mint_missing", "mint_foreign", "mint_ttl"}:
        original = storage.create_api_token

        def bad_create(user_id, token_sha256, **kwargs):
            if fault == "mint_ttl":
                kwargs["ttl_seconds"] = None
            row = original(_B if fault == "mint_foreign" else user_id, token_sha256, **kwargs)
            if fault == "mint_missing":
                with storage.transaction() as connection:
                    connection.execute("DELETE FROM api_tokens WHERE id=?", (row["id"],))
            return row

        monkeypatch.setattr(storage, "create_api_token", bad_create)
    elif fault in {"mint_secret", "ttl_false_refusal"}:
        original_request = TestClient.request

        def bad_response(self, method, url, **kwargs):
            if fault == "ttl_false_refusal" and method.upper() == "POST" and url == "/api/admin/tokens":
                # A reported refusal must also prove no credential was issued.
                kwargs["json"] = {**kwargs["json"], "ttl_seconds": 3600}
            response = original_request(self, method, url, **kwargs)
            if method.upper() == "POST" and url == "/api/admin/tokens" and response.status_code == 200:
                if fault == "ttl_false_refusal":
                    return httpx.Response(
                        400, json={"detail": "synthetic false refusal"}, request=response.request
                    )
                body = response.json()
                body["token"] = "jrc_wrong_synthetic_secret"
                return httpx.Response(200, json=body, request=response.request)
            return response

        monkeypatch.setattr(TestClient, "request", bad_response)
    elif fault == "revoke_unwritten":
        monkeypatch.setattr(storage, "revoke_api_token", lambda *args, **kwargs: True)
    else:
        original_revoke = storage.revoke_api_token

        def bad_revoke(token_id, **kwargs):
            result = original_revoke(token_id, **kwargs)
            original_revoke(ctx["records"]["b"])
            return result

        monkeypatch.setattr(storage, "revoke_api_token", bad_revoke)
    scenario = {
        "list": _assert_listing,
        "mint": _assert_mint,
        "revoke": _assert_revoke,
        "ttl": test_token_mint_refuses_fractional_ttl_without_persisting_a_credential,
    }[fault.split("_")[0]]
    with pytest.raises(AssertionError, match=code):
        scenario(ctx)


_REVIEW_FAULTS = (
    ("mint_touch", "token_mint_preserves_siblings"),
    ("revoke_missing_audit", "token_revoke_no_extra_audit"),
    ("ordinary_touch", "token_refusal_no_target_changes"),
    ("admin_touch", "token_admin_owner_refusal_no_effect"),
    ("delegated_list", "token_list_exact_metadata"),
    ("delegated_touch", "token_authenticated_touch_in_request_window"),
    ("revoked_auth", "token_revoke_auth_denied"),
)


@pytest.mark.parametrize("fault,code", _REVIEW_FAULTS, ids=[row[0] for row in _REVIEW_FAULTS])
def test_token_review_controls_catch_scope_audit_and_delegated_listing_faults(
    token_http, monkeypatch, fault, code
):
    ctx, storage = token_http, token_http["storage"]
    if fault == "mint_touch":
        original_create = storage.create_api_token

        def touch_every_sibling(user_id, token_sha256, **kwargs):
            row = original_create(user_id, token_sha256, **kwargs)
            with storage.transaction() as connection:
                connection.execute(
                    "UPDATE api_tokens SET last_used_at='2099-01-01T00:00:00+00:00' WHERE id<>?", (row["id"],)
                )
            return row

        monkeypatch.setattr(storage, "create_api_token", touch_every_sibling)
    elif fault == "revoked_auth":
        original_find = storage.find_api_token

        def admit_revoked(token_sha256):
            found = original_find(token_sha256)
            if found is not None:
                return found
            row = storage.execute("SELECT * FROM api_tokens WHERE token_sha256=?", (token_sha256,)).fetchone()
            return dict(row) if row is not None else None

        monkeypatch.setattr(storage, "find_api_token", admit_revoked)
    else:
        original_request = TestClient.request

        def corrupt_after_actual_http(self, method, url, **kwargs):
            response = original_request(self, method, url, **kwargs)
            method = method.upper()
            auth = (kwargs.get("headers") or {}).get("Authorization")
            delegated = auth == "Bearer " + ctx["secrets"]["admin"]
            ordinary = auth == "Bearer " + ctx["secrets"]["a"]
            if (
                fault == "revoke_missing_audit"
                and method == "DELETE"
                and url == "/api/admin/tokens/missing-token"
            ):
                assert response.status_code == 404, "fault_setup_actual_missing_refusal"
                storage.log_audit(
                    AuditEntry(
                        id="audit_token_review_missing",
                        user_id=LEGACY_OWNER_USER_ID,
                        action="admin.token.revoke",
                        target_type="api_token",
                        target_id="missing-token",
                    )
                )
            if (
                fault in {"delegated_list", "delegated_touch"}
                and method == "GET"
                and url == "/api/admin/tokens"
                and delegated
            ):
                assert response.status_code == 200, "fault_setup_actual_delegated_list"
                if fault == "delegated_list":
                    body = response.json()
                    assert body["items"], "fault_setup_nonempty_list"
                    body["items"][0]["token_sha256"] = "synthetic-leaked-hash"
                    return httpx.Response(200, json=body, request=response.request)
                with storage.transaction() as connection:
                    connection.execute(
                        "UPDATE api_tokens SET last_used_at='2099-01-01T00:00:00+00:00' WHERE id=?",
                        (ctx["records"]["admin"],),
                    )
            collateral = (fault == "ordinary_touch" and ordinary and method == "GET") or (
                fault == "admin_touch" and delegated and method == "POST"
            )
            if collateral and url == "/api/admin/tokens" and response.status_code == 403:
                target = ctx["records"]["sibling" if ordinary else "b"]
                with storage.transaction() as connection:
                    connection.execute(
                        "UPDATE api_tokens SET last_used_at='2099-01-01T00:00:00+00:00' WHERE id=?", (target,)
                    )
            return response

        monkeypatch.setattr(TestClient, "request", corrupt_after_actual_http)
    with pytest.raises(AssertionError, match=code):
        if fault == "mint_touch":
            _assert_mint(ctx)
        elif fault in {"revoke_missing_audit", "revoked_auth"}:
            _assert_revoke(ctx)
        elif fault.startswith("delegated"):
            _assert_listing(ctx, delegated=True)
        else:
            test_token_management_refuses_anonymous_ordinary_and_owner_escalation_without_target_changes(ctx)


def _assert_ttl_http_refusal_is_atomic(ctx, *, json_value=None, raw_json="") -> None:
    before = _rows(ctx)
    audit_before = _audit(ctx["storage"], "admin.token.create")
    if raw_json:
        response = ctx["client"].post(
            "/api/admin/tokens",
            content=f'{{"user_id":"{_A}","ttl_seconds":{raw_json}}}',
            headers={**ctx["owner"], "Content-Type": "application/json"},
        )
    else:
        response = ctx["client"].post(
            "/api/admin/tokens",
            json={"user_id": _A, "ttl_seconds": json_value},
            headers=ctx["owner"],
        )
    assert response.status_code == 400, "token_ttl_http_refused"
    _equal(_rows(ctx), before, "token_ttl_http_no_credential")
    _equal(_audit(ctx["storage"], "admin.token.create"), audit_before, "token_ttl_http_no_audit")


@pytest.mark.parametrize(
    "invalid_ttl",
    [1.5, "not-an-integer", True],
    ids=["fractional", "invalid-string", "boolean"],
)
def test_token_inexact_or_invalid_http_ttl_is_atomic(token_http, invalid_ttl) -> None:
    _assert_ttl_http_refusal_is_atomic(token_http, json_value=invalid_ttl)


def test_token_nonfinite_http_ttl_is_atomic(token_http) -> None:
    for raw_json in ("NaN", "Infinity", "-Infinity"):
        _assert_ttl_http_refusal_is_atomic(token_http, raw_json=raw_json)


@pytest.mark.parametrize(
    "invalid_ttl",
    [1.0, "3600", True],
    ids=["integral-float", "numeric-string", "boolean"],
)
def test_token_storage_ttl_type_refusal_is_atomic(token_http, invalid_ttl) -> None:
    before = _rows(token_http)
    token_hash = hashlib.sha256(f"invalid-storage:{invalid_ttl!r}".encode()).hexdigest()
    with pytest.raises(ValueError, match="positive integer"):
        token_http["storage"].create_api_token(_A, token_hash, ttl_seconds=invalid_ttl)
    _equal(_rows(token_http), before, "token_ttl_storage_no_credential")


def _assert_compatible_http_ttl_mint(ctx, supplied_ttl, expected_seconds) -> None:
    storage = ctx["storage"]
    before = _rows(ctx)
    body = _body(
        ctx["client"].post(
            "/api/admin/tokens",
            json={"user_id": _A, "label": "compatible-ttl", "ttl_seconds": supplied_ttl},
            headers=ctx["owner"],
        )
    )
    row = storage.execute("SELECT * FROM api_tokens WHERE id=?", (body["id"],)).fetchone()
    assert row is not None, "token_compatible_ttl_persisted"
    row = dict(row)
    secret = body["token"]
    _equal(
        (body["user_id"], row["user_id"], row["created_by"]),
        (_A, _A, LEGACY_OWNER_USER_ID),
        "token_compatible_ttl_identity",
    )
    _equal(row["token_sha256"], hashlib.sha256(secret.encode()).hexdigest(), "token_compatible_ttl_hash")
    _equal(row["expires_at"], body["expires_at"], "token_compatible_ttl_expiry_response")
    _equal(
        (
            datetime.fromisoformat(row["expires_at"]) - datetime.fromisoformat(row["created_at"])
        ).total_seconds(),
        expected_seconds,
        "token_compatible_ttl_exact_lifetime",
    )
    after = _rows(ctx)
    _equal([item for item in after if item["id"] != body["id"]], before, "token_compatible_ttl_siblings")
    assert len(after) == len(before) + 1 and secret not in json.dumps(after), "token_compatible_ttl_hash_only"
    audit = [
        dict(item)
        for item in storage.execute(
            "SELECT * FROM audit_log WHERE action='admin.token.create' AND target_id=?",
            (body["id"],),
        ).fetchall()
    ]
    assert (
        len(audit) == 1
        and audit[0]["user_id"] == LEGACY_OWNER_USER_ID
        and audit[0]["target_type"] == "api_token"
    ), "token_compatible_ttl_audit"
    assert secret not in json.dumps(audit) and row["token_sha256"] not in json.dumps(audit), (
        "token_compatible_ttl_audit_secret"
    )
    before_auth, started = _rows(ctx), datetime.now(UTC)
    me = _body(ctx["client"].get("/api/me", headers={"Authorization": f"Bearer {secret}"}))
    _assert_only_auth_touch(ctx, before_auth, body["id"], started, "token_compatible_ttl_auth_touch")
    _equal(me["actor"]["user_id"], _A, "token_compatible_ttl_auth_person")


@pytest.mark.parametrize(
    "supplied_ttl,expected_seconds",
    [(3600.0, 3600), ("3600", 3600)],
    ids=["integral-float", "numeric-string"],
)
def test_token_integral_http_ttl_compatibility(token_http, supplied_ttl, expected_seconds) -> None:
    _assert_compatible_http_ttl_mint(token_http, supplied_ttl, expected_seconds)


def test_token_one_second_integral_float_is_normalized_exactly(token_http) -> None:
    body = _body(
        token_http["client"].post(
            "/api/admin/tokens",
            json={"user_id": _A, "ttl_seconds": 1.0},
            headers=token_http["owner"],
        )
    )
    row = token_http["storage"].execute("SELECT * FROM api_tokens WHERE id=?", (body["id"],)).fetchone()
    assert row is not None, "token_one_second_float_persisted"
    _equal(
        (
            datetime.fromisoformat(row["expires_at"]) - datetime.fromisoformat(row["created_at"])
        ).total_seconds(),
        1,
        "token_one_second_float_exact_lifetime",
    )
