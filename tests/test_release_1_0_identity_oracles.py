"""Exact HTTP, persistence, person and authority oracles for login identities."""

from __future__ import annotations

import json
import time
import uuid
from copy import deepcopy
from dataclasses import replace
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from friday.permissions import LEGACY_OWNER_USER_ID
from friday.security import sign_bridge_request
from friday.server import create_app
from tests.test_api_tokens import _issue
from tests.test_release_1_0_conversation_oracles import _equal

_A = "local:r10-identity-a"
_B = "local:r10-identity-b"
_ADMIN = "local:r10-identity-admin"
_ORDINARY = "local:r10-identity-ordinary"
_FIXTURE_ACTOR = "local:r10-identity-fixture"
_A_EMAIL = "alice-private-028"
_A_SSO = "alice-sso-private-028"
_B_EMAIL = "bob-private-028"
_OWNER_TELEGRAM = "5002"
_NEW_TELEGRAM = "5001"
_MISSING_EXTERNAL = "missing-private-028"
_IDENTITY_KEYS = {"source", "external_id", "user_id", "linked_by", "created_at"}
_AUDIT_ACTIONS = ("admin.identities.read", "admin.identity.link", "admin.identity.unlink")


@pytest.fixture
def identity_http(settings):
    assert not settings.llm_enabled and not settings.workers_enabled
    app = create_app(
        replace(
            settings,
            shared_archive=True,
            telegram_allowed_chat_ids=[int(_NEW_TELEGRAM)],
            telegram_owner_chat_ids=[],
        )
    )
    with TestClient(app) as client:
        storage = app.state.storage
        headers: dict[str, dict[str, str]] = {}
        for key, person, preset in (
            ("a", _A, "user"),
            ("b", _B, "user"),
            ("admin", _ADMIN, "admin"),
            ("ordinary", _ORDINARY, "user"),
        ):
            secret = f"jrc_synthetic_identity_oracle_{key}_028"
            _issue(storage, person, preset, secret)
            storage.update_user(person, metadata_json={"preserve": key})
            headers[key] = {"Authorization": f"Bearer {secret}"}
        for source, external_id, person in (
            ("email", _A_EMAIL, _A),
            ("sso", _A_SSO, _A),
            ("email", _B_EMAIL, _B),
            ("email", _NEW_TELEGRAM, _B),
            ("telegram", _OWNER_TELEGRAM, LEGACY_OWNER_USER_ID),
        ):
            storage.link_identity(source, external_id, person, linked_by=_FIXTURE_ACTOR)
        yield {
            "client": client,
            "storage": storage,
            "settings": settings,
            "owner": {"Authorization": f"Bearer {settings.api_token}"},
            "headers": headers,
        }


def _identity_rows(ctx) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in ctx["storage"]
        .execute("SELECT * FROM user_identities ORDER BY source, external_id")
        .fetchall()
    ]


def _authority_rows(ctx) -> dict[str, list[dict[str, Any]]]:
    accounts = [dict(row) for row in ctx["storage"].execute("SELECT * FROM users ORDER BY id").fetchall()]
    for row in accounts:
        if row["id"] == LEGACY_OWNER_USER_ID:
            # Only configured-owner authentication touches this account.
            row.pop("updated_at")
            row.pop("last_seen_at")
    overrides = [
        dict(row)
        for row in ctx["storage"]
        .execute("SELECT * FROM user_permission_overrides ORDER BY user_id,security_id")
        .fetchall()
    ]
    return {"accounts": accounts, "overrides": overrides}


def _assert_authority_unchanged(ctx, before, *, bridge_auth=False):
    after = _authority_rows(ctx)
    if bridge_auth:
        # Actual signed bridge auth records the literal chat/language metadata
        # and activity times. All original metadata and authority remain exact.
        before = deepcopy(before)
        target = next(row for row in before["accounts"] if row["id"] == _A)
        target["metadata_json"] = json.dumps(
            {**json.loads(target["metadata_json"]), "chat_id": _NEW_TELEGRAM, "language_code": None},
            sort_keys=True,
        )

        def projected(rows):
            return [
                {
                    k: v
                    for k, v in row.items()
                    if not (row["id"] == _A and k in {"updated_at", "last_seen_at"})
                }
                for row in rows
            ]

        _equal(
            projected(after["accounts"]),
            projected(before["accounts"]),
            "identity_preserves_account_authority",
        )
    else:
        _equal(after["accounts"], before["accounts"], "identity_preserves_account_authority")
    _equal(after["overrides"], before["overrides"], "identity_preserves_permission_overrides")


def _identity_row(ctx, source: str, external_id: str) -> dict[str, Any] | None:
    rows = [
        row
        for row in _identity_rows(ctx)
        if row["source"].casefold() == source.casefold() and row["external_id"] == external_id
    ]
    assert len(rows) <= 1, "identity_row_ambiguous"
    return rows[0] if rows else None


def _without_identity(rows: list[dict[str, Any]], source: str, external_id: str) -> list[dict[str, Any]]:
    return [
        row
        for row in rows
        if (row["source"].casefold(), row["external_id"]) != (source.casefold(), external_id)
    ]


def _audit_rows(ctx, action: str) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in ctx["storage"]
        .execute("SELECT * FROM audit_log WHERE action=? ORDER BY rowid", (action,))
        .fetchall()
    ]


def _success_audits(ctx) -> dict[str, list[dict[str, Any]]]:
    return {action: _audit_rows(ctx, action) for action in _AUDIT_ACTIONS}


def _json_or_none(value: str | None) -> Any:
    return None if value is None else json.loads(value)


def _audit_semantic(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "user_id": row["user_id"],
        "action": row["action"],
        "target_type": row["target_type"],
        "target_id": row["target_id"],
        "before": _json_or_none(row["before_json"]),
        "after": _json_or_none(row["after_json"]),
    }


def _new_audit(ctx, action: str, before: list[dict[str, Any]], expected: dict[str, Any], code: str) -> None:
    rows = _audit_rows(ctx, action)
    _equal([_audit_semantic(row) for row in rows[len(before) :]], [expected], code)


def _read_audit(actor: str, target: str, after: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "user_id": actor,
        "action": "admin.identities.read",
        "target_type": "user",
        "target_id": target,
        "before": None,
        "after": after,
    }


def _link_audit_after(link: dict[str, Any]) -> dict[str, Any]:
    return {
        "source_chars": len(link["source"]),
        "external_id_chars": len(link["external_id"]),
        "user_id": link["user_id"],
        "created_at": link["created_at"],
        "private_fields_count": 1,
        "private_chars": len(link["linked_by"]),
    }


def _link_audit(*, actor: str, target: str, link: dict[str, Any], previous: str | None) -> dict[str, Any]:
    return {
        "user_id": actor,
        "action": "admin.identity.link",
        "target_type": "user",
        "target_id": target,
        "before": {"user_id": previous} if previous else None,
        "after": _link_audit_after(link),
    }


def _unlink_audit(actor: str, target: str, raw_source: str) -> dict[str, Any]:
    return {
        "user_id": actor,
        "action": "admin.identity.unlink",
        "target_type": "user",
        "target_id": target,
        "before": {"source_chars": len(raw_source)},
        "after": None,
    }


def _ok(response, code: str) -> dict[str, Any]:
    _equal(response.status_code, 200, code)
    body = response.json()
    assert isinstance(body, dict), code + "_object"
    return body


def _assert_list_response(response, expected: list[dict[str, Any]], code: str) -> None:
    body = _ok(response, code + "_status")
    _equal(set(body), {"items", "count"}, code + "_shape")
    _equal(body["items"], expected, code + "_items")
    _equal(body["count"], len(expected), code + "_count")
    assert all(set(item) == _IDENTITY_KEYS for item in body["items"]), code + "_row_shape"


def _assert_listing(ctx) -> None:
    authority = _authority_rows(ctx)
    client = ctx["client"]
    rows = _identity_rows(ctx)

    before = _audit_rows(ctx, "admin.identities.read")
    response = client.get("/api/admin/identities", headers=ctx["headers"]["admin"])
    _assert_list_response(response, rows, "identity_list_global")
    _new_audit(
        ctx,
        "admin.identities.read",
        before,
        _read_audit(_ADMIN, "*", {"scope": "all_tenants"}),
        "identity_list_global_audit",
    )

    expected_a = [row for row in rows if row["user_id"] == _A]
    before = _audit_rows(ctx, "admin.identities.read")
    response = client.get("/api/admin/identities", params={"user_id": _A}, headers=ctx["headers"]["admin"])
    _assert_list_response(response, expected_a, "identity_list_person")
    _new_audit(
        ctx,
        "admin.identities.read",
        before,
        _read_audit(_ADMIN, _A),
        "identity_list_person_audit",
    )

    before = _audit_rows(ctx, "admin.identities.read")
    response = client.get(
        "/api/admin/identities", params={"user_id": _ADMIN}, headers=ctx["headers"]["admin"]
    )
    _assert_list_response(response, [], "identity_list_own_person")
    _equal(_audit_rows(ctx, "admin.identities.read"), before, "identity_list_own_person_no_audit")
    _equal(_identity_rows(ctx), rows, "identity_list_no_persisted_effect")
    _assert_authority_unchanged(ctx, authority)


def _bridge_me(ctx, sender: str):
    path = "/api/me"
    timestamp = int(time.time())
    nonce = uuid.uuid4().hex
    signature = sign_bridge_request(
        ctx["settings"].telegram_bridge_secret,
        timestamp=timestamp,
        method="GET",
        path=path,
        external_user_id=sender,
        chat_id=sender,
        nonce=nonce,
        body=b"",
    )
    return ctx["client"].get(
        path,
        headers={
            "X-Friday-Timestamp": str(timestamp),
            "X-Friday-User": sender,
            "X-Friday-Chat": sender,
            "X-Friday-Nonce": nonce,
            "X-Friday-Signature": signature,
        },
    )


def _assert_owner_link(ctx) -> None:
    authority = _authority_rows(ctx)
    client, storage = ctx["client"], ctx["storage"]
    before_rows = _identity_rows(ctx)
    stable_account = {key: storage.get_user(_A)[key] for key in ("id", "source", "external_id", "preset_key")}
    payload = {
        "source": " TELEGRAM ",
        "external_id": f" {_NEW_TELEGRAM} ",
        "user_id": f" {_A} ",
    }

    audits = _audit_rows(ctx, "admin.identity.link")
    body = _ok(
        client.post("/api/admin/identities", headers=ctx["owner"], json=payload), "identity_link_status"
    )
    _equal(set(body), {"identity"}, "identity_link_shape")
    link = body["identity"]
    _equal(set(link), _IDENTITY_KEYS, "identity_link_row_shape")
    _equal(
        (link["source"], link["external_id"], link["user_id"], link["linked_by"]),
        ("telegram", _NEW_TELEGRAM, _A, LEGACY_OWNER_USER_ID),
        "identity_link_response_fields",
    )
    persisted = _identity_row(ctx, "telegram", _NEW_TELEGRAM)
    assert persisted is not None, "identity_link_persisted"
    _equal(persisted["user_id"], _A, "identity_link_person_binding")
    _equal(persisted, link, "identity_link_http_storage")
    _equal(
        _without_identity(_identity_rows(ctx), "telegram", _NEW_TELEGRAM),
        before_rows,
        "identity_link_preserves_other_links",
    )
    _equal(storage.resolve_identity("TeLeGrAm", _NEW_TELEGRAM), _A, "identity_link_resolves_person")
    _new_audit(
        ctx,
        "admin.identity.link",
        audits,
        _link_audit(actor=LEGACY_OWNER_USER_ID, target=_A, link=link, previous=None),
        "identity_link_audit",
    )
    assert _NEW_TELEGRAM not in (_audit_rows(ctx, "admin.identity.link")[-1]["after_json"] or ""), (
        "identity_link_audit_private_external_id"
    )

    audits = _audit_rows(ctx, "admin.identity.link")
    repeated = _ok(
        client.post("/api/admin/identities", headers=ctx["owner"], json=payload),
        "identity_link_repeat_status",
    )["identity"]
    _equal(_identity_row(ctx, "telegram", _NEW_TELEGRAM), repeated, "identity_link_repeat_persisted")
    _equal(
        (repeated["source"], repeated["external_id"], repeated["user_id"], repeated["linked_by"]),
        ("telegram", _NEW_TELEGRAM, _A, LEGACY_OWNER_USER_ID),
        "identity_link_repeat_fields",
    )
    _equal(repeated["user_id"], _A, "identity_link_repeat_person")
    _equal(
        _without_identity(_identity_rows(ctx), "telegram", _NEW_TELEGRAM),
        before_rows,
        "identity_link_repeat_preserves_others",
    )
    _equal(len(_identity_rows(ctx)), len(before_rows) + 1, "identity_link_repeat_one_row")
    _new_audit(
        ctx,
        "admin.identity.link",
        audits,
        _link_audit(actor=LEGACY_OWNER_USER_ID, target=_A, link=repeated, previous=_A),
        "identity_link_repeat_audit",
    )

    _assert_authority_unchanged(ctx, authority)
    rows_before_auth = _identity_rows(ctx)
    me = _ok(_bridge_me(ctx, _NEW_TELEGRAM), "identity_link_bridge_auth_status")
    _equal(
        me["actor"],
        {"user_id": _A, "preset_key": "user", "source": "telegram-bridge"},
        "identity_link_bridge_person",
    )
    _equal(me["user"]["id"], _A, "identity_link_bridge_user")
    _equal(
        {key: storage.get_user(_A)[key] for key in stable_account},
        stable_account,
        "identity_link_bridge_preserves_account_identity",
    )
    _equal(_identity_rows(ctx), rows_before_auth, "identity_link_bridge_preserves_links")
    _assert_authority_unchanged(ctx, authority, bridge_auth=True)


def _assert_owner_reassign(ctx) -> None:
    authority = _authority_rows(ctx)
    client, storage = ctx["client"], ctx["storage"]
    before = _identity_rows(ctx)
    old = _identity_row(ctx, "email", _A_EMAIL)
    assert old is not None
    audits = _audit_rows(ctx, "admin.identity.link")
    response = client.post(
        "/api/admin/identities",
        headers=ctx["owner"],
        json={"source": " EMAIL ", "external_id": f" {_A_EMAIL} ", "user_id": _B},
    )
    body = _ok(response, "identity_reassign_status")
    _equal(set(body), {"identity"}, "identity_reassign_shape")
    link = body["identity"]
    _equal(
        (link["source"], link["external_id"], link["user_id"], link["linked_by"]),
        ("email", _A_EMAIL, _B, LEGACY_OWNER_USER_ID),
        "identity_reassign_response",
    )
    _equal(_identity_row(ctx, "email", _A_EMAIL), link, "identity_reassign_persisted")
    _equal(storage.resolve_identity("email", _A_EMAIL), _B, "identity_reassign_person")
    expected_others = _without_identity(before, "email", _A_EMAIL)
    actual_others = _without_identity(_identity_rows(ctx), "email", _A_EMAIL)
    _equal(actual_others, expected_others, "identity_reassign_preserves_other_links")
    _equal(len(_identity_rows(ctx)), len(before), "identity_reassign_one_row")
    _new_audit(
        ctx,
        "admin.identity.link",
        audits,
        _link_audit(actor=LEGACY_OWNER_USER_ID, target=_B, link=link, previous=_A),
        "identity_reassign_audit",
    )
    _assert_authority_unchanged(ctx, authority)


def _assert_unlink(ctx) -> None:
    authority = _authority_rows(ctx)
    client, storage = ctx["client"], ctx["storage"]
    before = _identity_rows(ctx)
    removed = _identity_row(ctx, "email", _B_EMAIL)
    assert removed is not None
    audits = _audit_rows(ctx, "admin.identity.unlink")
    path = f"/api/admin/identities/EMAIL/{_B_EMAIL}"
    body = _ok(client.delete(path, headers=ctx["owner"]), "identity_unlink_status")
    _equal(body, {"status": "unlinked"}, "identity_unlink_response")
    _equal(storage.resolve_identity("email", _B_EMAIL), None, "identity_unlink_removed")
    expected = [row for row in before if row != removed]
    _equal(_identity_rows(ctx), expected, "identity_unlink_preserves_other_links")
    _new_audit(
        ctx,
        "admin.identity.unlink",
        audits,
        _unlink_audit(LEGACY_OWNER_USER_ID, _B, "EMAIL"),
        "identity_unlink_audit",
    )
    assert _B_EMAIL not in (_audit_rows(ctx, "admin.identity.unlink")[-1]["before_json"] or ""), (
        "identity_unlink_audit_private_external_id"
    )

    audits = _audit_rows(ctx, "admin.identity.unlink")
    response = client.delete(path, headers=ctx["owner"])
    _equal(response.status_code, 404, "identity_unlink_repeat_status")
    _equal(_identity_rows(ctx), expected, "identity_unlink_repeat_no_effect")
    _equal(_audit_rows(ctx, "admin.identity.unlink"), audits, "identity_unlink_repeat_no_audit")

    missing_path = f"/api/admin/identities/sso/{_MISSING_EXTERNAL}"
    response = client.delete(missing_path, headers=ctx["owner"])
    _equal(response.status_code, 404, "identity_unlink_missing_status")
    _equal(_identity_rows(ctx), expected, "identity_unlink_missing_no_effect")
    _equal(_audit_rows(ctx, "admin.identity.unlink"), audits, "identity_unlink_missing_no_audit")
    _assert_authority_unchanged(ctx, authority)


def _assert_invalid(ctx) -> None:
    authority = _authority_rows(ctx)
    before_rows = _identity_rows(ctx)
    before_audits = _success_audits(ctx)
    requests = (
        {"json": {}},
        {"json": {"source": " ", "external_id": "invalid-028", "user_id": _A}},
        {"json": {"source": "email", "external_id": " ", "user_id": _A}},
        {"json": {"source": "email", "external_id": "invalid-028", "user_id": " "}},
        {"json": {"source": "email", "external_id": "invalid-028", "user_id": "missing-person-028"}},
        {"json": {"source": "email", "external_id": "invalid-028", "user_id": "bad person id"}},
        {"json": []},
        {"content": b"{", "headers": {"Content-Type": "application/json"}},
    )
    for kwargs in requests:
        headers = {**ctx["owner"], **kwargs.pop("headers", {})}
        response = ctx["client"].post("/api/admin/identities", headers=headers, **kwargs)
        _equal(response.status_code, 400, "identity_invalid_status")
        _equal(_identity_rows(ctx), before_rows, "identity_invalid_no_effect")
        _assert_authority_unchanged(ctx, authority)
        _equal(_success_audits(ctx), before_audits, "identity_invalid_no_success_audit")


def _assert_refusals(ctx) -> None:
    authority = _authority_rows(ctx)
    client = ctx["client"]
    before_rows = _identity_rows(ctx)
    before_audits = _success_audits(ctx)
    denied_new = {"source": "email", "external_id": "denied-private-028", "user_id": _A}
    for headers, status in (({}, 401), (ctx["headers"]["ordinary"], 403)):
        for method, path, kwargs in (
            ("GET", "/api/admin/identities", {}),
            ("POST", "/api/admin/identities", {"json": denied_new}),
            ("DELETE", f"/api/admin/identities/email/{_B_EMAIL}", {}),
        ):
            response = client.request(method, path, headers=headers, **kwargs)
            _equal(response.status_code, status, "identity_authority_refusal")
            _equal(_identity_rows(ctx), before_rows, "identity_authority_refusal_no_effect")
            _assert_authority_unchanged(ctx, authority)
            _equal(_success_audits(ctx), before_audits, "identity_authority_refusal_no_success_audit")

    for method, path, kwargs in (
        (
            "POST",
            "/api/admin/identities",
            {
                "json": {
                    "source": "email",
                    "external_id": "owner-new-private-028",
                    "user_id": LEGACY_OWNER_USER_ID,
                }
            },
        ),
        ("DELETE", f"/api/admin/identities/telegram/{_OWNER_TELEGRAM}", {}),
    ):
        response = client.request(method, path, headers=ctx["headers"]["admin"], **kwargs)
        _equal(response.status_code, 403, "identity_delegated_owner_refusal")
        _equal(_identity_rows(ctx), before_rows, "identity_delegated_owner_no_effect")
        _assert_authority_unchanged(ctx, authority)
        _equal(_success_audits(ctx), before_audits, "identity_delegated_owner_no_success_audit")


def test_identity_listing_has_exact_global_and_person_filters_counts_order_and_audit(identity_http):
    _assert_listing(identity_http)


def test_identity_owner_filter_audit_keeps_the_person_target_in_a_shared_archive(identity_http):
    _assert_owner_person_listing(identity_http)


def _assert_owner_person_listing(ctx):
    authority = _authority_rows(ctx)
    identities = _identity_rows(ctx)
    expected = [row for row in identities if row["user_id"] == LEGACY_OWNER_USER_ID]
    before = _audit_rows(ctx, "admin.identities.read")
    response = ctx["client"].get(
        "/api/admin/identities",
        params={"user_id": LEGACY_OWNER_USER_ID},
        headers=ctx["headers"]["admin"],
    )
    _assert_authority_unchanged(ctx, authority)
    _equal(_identity_rows(ctx), identities, "identity_owner_filter_preserves_links")
    _assert_list_response(response, expected, "identity_owner_filter")
    _new_audit(
        ctx,
        "admin.identities.read",
        before,
        _read_audit(_ADMIN, LEGACY_OWNER_USER_ID),
        "identity_owner_filter_person_audit",
    )


def test_identity_link_and_repeat_persist_one_person_binding_preserve_siblings_and_audit(identity_http):
    _assert_owner_link(identity_http)


def test_owner_reassignment_moves_only_the_requested_identity_and_audits_the_previous_person(identity_http):
    _assert_owner_reassign(identity_http)


def test_identity_unlink_repeat_and_missing_remove_once_preserve_siblings_and_audit(identity_http):
    _assert_unlink(identity_http)


def test_identity_invalid_requests_have_no_persisted_or_success_audit_effect(identity_http):
    _assert_invalid(identity_http)


def test_identity_endpoints_refuse_anonymous_ordinary_and_delegated_owner_mutations_without_effects(
    identity_http,
):
    _assert_refusals(identity_http)


def test_shared_archive_link_is_attributed_to_the_acting_person(identity_http):
    _assert_delegated_link(identity_http)


def _assert_delegated_link(ctx):
    authority = _authority_rows(ctx)
    identities = _identity_rows(ctx)
    before = _audit_rows(ctx, "admin.identity.link")
    response = ctx["client"].post(
        "/api/admin/identities",
        headers=ctx["headers"]["admin"],
        json={"source": "sso", "external_id": "delegated-private-028", "user_id": _A},
    )
    _assert_authority_unchanged(ctx, authority)
    others = _without_identity(_identity_rows(ctx), "sso", "delegated-private-028")
    _equal(others, identities, "identity_delegated_preserves_other_links")
    _equal(len(_identity_rows(ctx)), len(identities) + 1, "identity_delegated_one_link")
    body = _ok(response, "identity_delegated_link_status")
    link = body["identity"]
    _equal(_identity_row(ctx, "sso", "delegated-private-028"), link, "identity_delegated_link_persisted")
    _equal(link["user_id"], _A, "identity_delegated_link_person")
    _new_audit(
        ctx,
        "admin.identity.link",
        before,
        _link_audit(actor=_ADMIN, target=_A, link=link, previous=None),
        "identity_delegated_link_audit",
    )
    _equal(link["linked_by"], _ADMIN, "identity_delegated_link_personal_attribution")


def test_delegated_admin_cannot_reassign_an_owner_bound_identity(identity_http):
    _assert_owner_rebind_refusal(identity_http)


def test_identity_rebind_guards_both_accounts_and_writes_in_one_transaction(identity_http, monkeypatch):
    ctx = identity_http
    storage = ctx["storage"]
    from friday.admin_api import _users as user_routes

    observed = []
    original_resolve = storage.resolve_identity
    original_protect = user_routes._protect_owner_target
    original_link = storage.link_identity

    def resolve(source, external_id):
        if source == "email" and external_id == _B_EMAIL:
            observed.append(("resolve", storage.conn.in_transaction))
        return original_resolve(source, external_id)

    def protect(request, user_id):
        observed.append(("guard:" + user_id, storage.conn.in_transaction))
        return original_protect(request, user_id)

    def link(source, external_id, user_id, **kwargs):
        if source == "email" and external_id == _B_EMAIL:
            observed.append(("write", storage.conn.in_transaction))
        return original_link(source, external_id, user_id, **kwargs)

    monkeypatch.setattr(storage, "resolve_identity", resolve)
    monkeypatch.setattr(user_routes, "_protect_owner_target", protect)
    monkeypatch.setattr(storage, "link_identity", link)
    response = ctx["client"].post(
        "/api/admin/identities",
        headers=ctx["headers"]["admin"],
        json={"source": "email", "external_id": _B_EMAIL, "user_id": _A},
    )
    _equal(response.status_code, 200, "identity_atomic_rebind_status")
    _equal(
        observed,
        [
            ("resolve", True),
            ("guard:" + _B, True),
            ("guard:" + _A, True),
            ("write", True),
        ],
        "identity_atomic_guard_write",
    )


def _assert_owner_rebind_refusal(ctx):
    authority = _authority_rows(ctx)
    before_rows = _identity_rows(ctx)
    before_audits = _success_audits(ctx)
    response = ctx["client"].post(
        "/api/admin/identities",
        headers=ctx["headers"]["admin"],
        json={"source": "telegram", "external_id": _OWNER_TELEGRAM, "user_id": _A},
    )
    _assert_authority_unchanged(ctx, authority)
    _equal(
        (response.status_code, _identity_rows(ctx), _success_audits(ctx)),
        (403, before_rows, before_audits),
        "identity_owner_rebind_refusal_no_effect",
    )


_FAULTS = (
    ("list_filter", "identity_list_person_items"),
    ("list_output", "identity_list_global_count"),
    ("list_audit", "identity_list_global_audit"),
    ("link_unwritten", "identity_link_persisted"),
    ("link_wrong_person", "identity_link_person_binding"),
    ("link_collateral", "identity_link_preserves_other_links"),
    ("link_response", "identity_link_response_fields"),
    ("link_audit", "identity_link_audit"),
    ("link_repeat_provenance", "identity_link_repeat_fields"),
    ("unlink_unwritten", "identity_unlink_removed"),
    ("unlink_collateral", "identity_unlink_preserves_other_links"),
    ("unlink_response", "identity_unlink_response"),
    ("unlink_audit", "identity_unlink_audit"),
    ("invalid_false_refusal", "identity_invalid_no_effect"),
    ("missing_false_refusal", "identity_unlink_missing_no_effect"),
)


@pytest.mark.parametrize("fault,code", _FAULTS, ids=[item[0] for item in _FAULTS])
def test_identity_oracles_detect_corrupted_http_audit_and_actual_link_mutations(
    identity_http, monkeypatch, fault, code
):
    ctx = identity_http
    storage = ctx["storage"]
    original_request = TestClient.request

    if fault == "list_filter":
        original_list = storage.list_identities

        def leaky_list(user_id=None):
            return original_list(None if user_id == _A else user_id)

        monkeypatch.setattr(storage, "list_identities", leaky_list)

    if fault.endswith("_audit"):
        omitted = {
            "list_audit": "admin.identities.read",
            "link_audit": "admin.identity.link",
            "unlink_audit": "admin.identity.unlink",
        }[fault]
        original_audit = storage.log_audit

        def omit_selected(entry):
            if entry.action == omitted:
                return entry
            return original_audit(entry)

        monkeypatch.setattr(storage, "log_audit", omit_selected)

    injected = False
    new_link_calls = 0

    def altered(self, method, url, **kwargs):
        nonlocal injected, new_link_calls
        method = method.upper()
        body = kwargs.get("json")

        if (
            fault == "invalid_false_refusal"
            and method == "POST"
            and url == "/api/admin/identities"
            and body == {}
        ):
            changed = dict(kwargs)
            changed["json"] = {"source": "email", "external_id": "false-refusal-028", "user_id": _A}
            response = original_request(self, method, url, **changed)
            assert response.status_code == 200, "identity_fault_setup_invalid_write"
            return httpx.Response(400, json={"detail": "synthetic refusal"}, request=response.request)

        if fault == "missing_false_refusal" and method == "DELETE" and url.endswith(_MISSING_EXTERNAL):
            response = original_request(
                self,
                "DELETE",
                f"/api/admin/identities/sso/{_A_SSO}",
                **kwargs,
            )
            assert response.status_code == 200, "identity_fault_setup_missing_write"
            return httpx.Response(404, json={"detail": "synthetic missing"}, request=response.request)

        response = original_request(self, method, url, **kwargs)

        global_list = (
            method == "GET"
            and url == "/api/admin/identities"
            and not kwargs.get("params")
            and response.status_code == 200
        )
        if fault == "list_output" and global_list:
            altered_body = response.json()
            altered_body["count"] += 1
            return httpx.Response(200, json=altered_body, request=response.request)

        new_link = (
            method == "POST"
            and url == "/api/admin/identities"
            and isinstance(body, dict)
            and str(body.get("external_id") or "").strip() == _NEW_TELEGRAM
            and response.status_code == 200
        )
        if new_link:
            new_link_calls += 1
        if fault == "link_repeat_provenance" and new_link_calls == 2:
            wrong_actor = "f" * len(LEGACY_OWNER_USER_ID)
            with storage.transaction() as connection:
                connection.execute(
                    "UPDATE user_identities SET linked_by=? WHERE source=? AND external_id=?",
                    (wrong_actor, "telegram", _NEW_TELEGRAM),
                )
            altered_body = response.json()
            altered_body["identity"]["linked_by"] = wrong_actor
            return httpx.Response(200, json=altered_body, request=response.request)
        if new_link and not injected:
            injected = True
            if fault == "link_unwritten":
                storage.unlink_identity("telegram", _NEW_TELEGRAM)
            elif fault == "link_wrong_person":
                storage.link_identity("telegram", _NEW_TELEGRAM, _B, linked_by="synthetic-fault")
            elif fault == "link_collateral":
                storage.unlink_identity("sso", _A_SSO)
            elif fault == "link_response":
                altered_body = response.json()
                altered_body["identity"]["external_id"] = "invented-output-028"
                return httpx.Response(200, json=altered_body, request=response.request)

        unlink_path = f"/api/admin/identities/EMAIL/{_B_EMAIL}"
        if method == "DELETE" and url == unlink_path and response.status_code == 200 and not injected:
            injected = True
            if fault == "unlink_unwritten":
                storage.link_identity("email", _B_EMAIL, _B, linked_by=_FIXTURE_ACTOR)
            elif fault == "unlink_collateral":
                storage.unlink_identity("sso", _A_SSO)
            elif fault == "unlink_response":
                return httpx.Response(200, json={"status": "still-linked"}, request=response.request)
        return response

    monkeypatch.setattr(TestClient, "request", altered)
    scenario = {
        "list": _assert_listing,
        "link": _assert_owner_link,
        "unlink": _assert_unlink,
        "invalid": _assert_invalid,
        "missing": _assert_unlink,
    }[fault.split("_", 1)[0]]
    with pytest.raises(AssertionError, match=code):
        scenario(ctx)


@pytest.mark.parametrize("fault", ["refusal_account", "refusal_override", "link_account"])
def test_identity_review_oracles_catch_real_collateral_authority_mutations(identity_http, monkeypatch, fault):
    ctx = identity_http
    original = ctx["client"].request
    injected = False

    def request(method, url, **kwargs):
        nonlocal injected
        response = original(method, url, **kwargs)
        trigger = (
            response.status_code == 403
            if fault.startswith("refusal")
            else method.upper() == "POST" and url == "/api/admin/identities" and response.status_code == 200
        )
        if trigger and not injected:
            injected = True
            if fault == "refusal_override":
                with ctx["storage"].transaction() as connection:
                    connection.execute(
                        "INSERT INTO user_permission_overrides(user_id,security_id,effect,updated_at) VALUES(?,?,?,?)",
                        (_B, "admin.users.manage", "allow", "2024-01-01T00:00:00+00:00"),
                    )
            else:
                ctx["storage"].update_user(_B, preset_key="admin")
        return response

    monkeypatch.setattr(ctx["client"], "request", request)
    with pytest.raises(
        AssertionError,
        match="identity_preserves_permission_overrides"
        if fault == "refusal_override"
        else "identity_preserves_account_authority",
    ):
        (_assert_refusals if fault.startswith("refusal") else _assert_owner_link)(ctx)
    assert injected, "identity_review_fault_exercised"


@pytest.mark.parametrize(
    "path,fault",
    [
        (path, fault)
        for path in ("owner_list", "delegated_link", "owner_rebind")
        for fault in ("account", "override")
    ]
    + [(path, "other_identity") for path in ("owner_list", "delegated_link")],
    ids=[
        path + "_" + fault
        for path in ("owner_list", "delegated_link", "owner_rebind")
        for fault in ("account", "override")
    ]
    + [path + "_other_identity" for path in ("owner_list", "delegated_link")],
)
def test_identity_person_paths_detect_real_collateral_writes_before_existing_source_failures(
    identity_http, monkeypatch, path, fault
):
    ctx = identity_http
    original = ctx["client"].request
    injected = False

    def damaged_request(method, url, **kwargs):
        nonlocal injected
        response = original(method, url, **kwargs)
        assert url == "/api/admin/identities" and not injected, "identity_person_fault_exact_request"
        injected = True
        with ctx["storage"].transaction() as conn:
            if fault == "account":
                conn.execute("UPDATE users SET preset_key='admin' WHERE id=?", (_B,))
            elif fault == "override":
                conn.execute(
                    "INSERT INTO user_permission_overrides(user_id,security_id,effect,updated_at) VALUES(?,?,?,?)",
                    (_B, "admin.users.manage", "allow", "2024-01-01T00:00:00+00:00"),
                )
            else:
                conn.execute("DELETE FROM user_identities WHERE source='sso' AND external_id=?", (_A_SSO,))
        return response

    monkeypatch.setattr(ctx["client"], "request", damaged_request)
    code = {
        "account": "identity_preserves_account_authority",
        "override": "identity_preserves_permission_overrides",
        "other_identity": "identity_owner_filter_preserves_links"
        if path == "owner_list"
        else "identity_delegated_preserves_other_links",
    }[fault]
    oracle = {
        "owner_list": _assert_owner_person_listing,
        "delegated_link": _assert_delegated_link,
        "owner_rebind": _assert_owner_rebind_refusal,
    }[path]
    # Collateral checks run before the independently retained audit/provenance/
    # owner-refusal failures. A known product failure cannot satisfy this control.
    with pytest.raises(AssertionError, match=code):
        oracle(ctx)
    assert injected, "identity_person_fault_exercised"
