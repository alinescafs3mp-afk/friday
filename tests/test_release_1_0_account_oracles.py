"""Account lookup, authority and oversight observe actual HTTP and persisted state."""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import replace

import httpx
import pytest
from fastapi.testclient import TestClient

from friday.permissions import LEGACY_OWNER_USER_ID
from friday.server import create_app
from friday.storage.models import RawObject
from tests.test_api_tokens import _issue
from tests.test_release_1_0_conversation_oracles import _audit, _body, _equal

_A, _B = "local:r10-account-a", "local:r10-account-b"
_ADMIN, _WATCH = "local:r10-account-admin", "local:r10-account-watch"
_READ = "admin.users.read"
_PREFIX = "/api/admin/users/"
_ACTIONS = ("admin.user.preset", "admin.user.supervisor", "admin.permission.override")


@pytest.fixture
def account_http(settings, monkeypatch):
    assert not settings.llm_enabled and not settings.workers_enabled
    app = create_app(replace(settings, shared_archive=True, telegram_owner_chat_ids=[]))
    with TestClient(app) as client:
        storage = app.state.storage
        ctx = {
            "client": client,
            "storage": storage,
            "owner": {"Authorization": f"Bearer {settings.api_token}"},
        }
        for person, preset, name, username in (
            (_A, "user", "Саша", "sasha_one"),
            (_B, "user", "Саша", "sasha_two"),
            (_ADMIN, "admin", "Анна Королёва", "anna_unique"),
            (_WATCH, "user", "Наблюдатель", "observer_unique"),
        ):
            secret = "jrc_synthetic_account_" + username
            _issue(storage, person, preset, secret)
            # Positive mutations use explicit local origins. Separate required
            # tests below retain the nonlocal-origin preservation contract.
            storage.ensure_user(
                person,
                source="local",
                external_id=username,
                display_name=name,
                username=username,
                metadata={"preserve": person, "private_note": "PRIVATE-account"},
            )
            ctx[person] = {"Authorization": f"Bearer {secret}"}
        storage.set_permission_override(_WATCH, "admin.activity.read", "allow")
        storage.set_permission_override(_A, "admin.activity.read", "deny")
        _body(client.get("/api/me", headers=ctx["owner"]))
        ctx["audit_inputs"] = []
        writer = storage.log_audit

        def record_audit_input(entry):
            # Observe the real route's values before the privacy writer removes
            # private names/metadata. Persisted audit is checked separately.
            ctx["audit_inputs"].append(deepcopy(entry))
            return writer(entry)

        monkeypatch.setattr(storage, "log_audit", record_audit_input)
        yield ctx


def _accounts(ctx):
    rows = [dict(r) for r in ctx["storage"].execute("SELECT * FROM users ORDER BY id").fetchall()]
    for row in rows:
        if row["id"] == LEGACY_OWNER_USER_ID:
            # Only configured-owner authentication calls ensure_user. Personal
            # bearer auth touches tokens, not these account rows.
            row.pop("updated_at")
            row.pop("last_seen_at")
    return rows


def _overrides(ctx):
    return [
        dict(r)
        for r in ctx["storage"]
        .execute("SELECT * FROM user_permission_overrides ORDER BY user_id,security_id")
        .fetchall()
    ]


def _audit_delta(ctx, action, before, *, actor=LEGACY_OWNER_USER_ID, target=_A):
    after = _audit(ctx["storage"], action)
    _equal(after[:-1], before, "account_audit_history")
    _equal(len(after), len(before) + 1, "account_audit_count")
    row = after[-1]
    _equal(
        (row["user_id"], row["target_type"], row["target_id"]),
        (actor, "user", target),
        "account_audit_attribution",
    )
    assert "PRIVATE-account" not in str(row["before_json"]) + str(row["after_json"]), (
        "account_audit_private_note"
    )
    return row


def _audit_values(ctx, action):
    entries = [e for e in ctx["audit_inputs"] if e.action == action]
    assert entries, "account_audit_input_observed"
    return entries[-1]


def _preserved(ctx, before, *, target=None, changed=()):
    after = _accounts(ctx)
    _equal(
        [r for r in after if r["id"] != target],
        [r for r in before if r["id"] != target],
        "account_other_rows_preserved",
    )
    if target is not None:
        original = next(r for r in before if r["id"] == target)
        actual = next(r for r in after if r["id"] == target)
        _equal(
            {k: v for k, v in actual.items() if k not in changed},
            {k: v for k, v in original.items() if k not in changed},
            "account_target_fields_preserved",
        )


def _assert_resolve(ctx):
    before = _accounts(ctx)
    for query, wanted in (("Саша", {_A, _B}), ("anna_unique", {_ADMIN}), ("zxqv_no_person_729", set())):
        body = _body(ctx["client"].get(_PREFIX + "resolve", params={"name": query}, headers=ctx[_ADMIN]))
        _equal(set(body), {"query", "matches", "unambiguous"}, "account_resolve_shape")
        _equal(body["query"], query, "account_resolve_query")
        matches = body["matches"]
        _equal({r["user_id"] for r in matches}, wanted, "account_resolve_people")
        _equal(len(matches), len(wanted), "account_resolve_cardinality")
        for row in matches:
            person = next(r for r in before if r["id"] == row["user_id"])
            _equal(
                row,
                {
                    "user_id": person["id"],
                    "display_name": person["display_name"],
                    "username": person["username"],
                    "confidence": 1.0,
                    "method": "exact",
                    "matched_on": query,
                },
                "account_resolve_safe_exact_match",
            )
        _equal(body["unambiguous"], matches[0] if wanted == {_ADMIN} else None, "account_resolve_ambiguity")
        _preserved(ctx, before)


def test_account_resolution_returns_exact_people_and_no_arbitrary_ambiguous_winner(account_http):
    _assert_resolve(account_http)


def _assert_preset(ctx):
    client, storage = ctx["client"], ctx["storage"]
    for preset, status in (("admin", 200), ("user", 403)):
        before, audit = _accounts(ctx), _audit(storage, _ACTIONS[0])
        overrides = _overrides(ctx)
        body = _body(client.post(_PREFIX + _A + "/preset", headers=ctx["owner"], json={"preset_key": preset}))
        row = storage.get_user(_A)
        _equal(row["preset_key"], preset, "account_preset_persisted")
        _equal(body, {"user": row}, "account_preset_http")
        _preserved(ctx, before, target=_A, changed={"preset_key", "updated_at", "last_seen_at"})
        _equal(_overrides(ctx), overrides, "account_preset_preserves_overrides")
        _audit_delta(ctx, _ACTIONS[0], audit)
        entry = _audit_values(ctx, _ACTIONS[0])
        _equal(
            entry.before_json,
            next(r for r in before if r["id"] == _A),
            "account_preset_audit_before",
        )
        _equal(entry.after_json, row, "account_preset_audit_after")
        _equal(
            client.get(_PREFIX + "resolve", params={"name": "Саша"}, headers=ctx[_A]).status_code,
            status,
            "account_preset_effective_http",
        )


def test_account_preset_changes_persist_and_change_actual_personal_http_authority(account_http):
    _assert_preset(account_http)


def _assert_permission(ctx):
    client, storage = ctx["client"], ctx["storage"]
    # Both removing an explicit allow and inheriting a preset grant are observed.
    for preset, effect, status in (
        ("user", "allow", 200),
        ("user", "deny", 403),
        ("user", "inherit", 403),
        ("admin", "deny", 403),
        ("admin", "inherit", 200),
    ):
        storage.update_user(_A, preset_key=preset)
        before, audit = _accounts(ctx), _audit(storage, _ACTIONS[2])
        prior_overrides = storage.get_permission_overrides(_A)
        other = [r for r in _overrides(ctx) if r["user_id"] != _A]
        wanted = {"admin.activity.read": "deny", **({} if effect == "inherit" else {_READ: effect})}
        body = _body(
            client.put(_PREFIX + _A + "/permissions/" + _READ, headers=ctx["owner"], json={"effect": effect})
        )
        _equal(storage.get_permission_overrides(_A), wanted, "account_permission_persisted")
        _equal(body, {"user_id": _A, "overrides": wanted}, "account_permission_http")
        _equal(
            [r for r in _overrides(ctx) if r["user_id"] != _A], other, "account_permission_other_overrides"
        )
        _preserved(ctx, before, target=_A, changed={"updated_at", "last_seen_at"})
        _audit_delta(ctx, _ACTIONS[2], audit)
        entry = _audit_values(ctx, _ACTIONS[2])
        _equal(entry.before_json, prior_overrides, "account_permission_audit_before")
        _equal(entry.after_json, wanted, "account_permission_audit_after")
        _equal(
            client.get(_PREFIX + "resolve", params={"name": "Саша"}, headers=ctx[_A]).status_code,
            status,
            "account_permission_effective_http",
        )


def test_account_allow_deny_inherit_preserve_other_accounts_and_change_real_http(account_http):
    _assert_permission(account_http)


@pytest.mark.parametrize(
    "route,payload",
    [("preset", {"preset_key": "admin"}), ("permissions/" + _READ, {"effect": "allow"})],
    ids=["preset", "permission"],
)
def test_account_authority_mutations_preserve_nonlocal_origin(account_http, route, payload):
    ctx = account_http
    ctx["storage"].ensure_user(_A, source="admin", external_id="origin-271")
    before = ctx["storage"].get_user(_A)
    response = ctx["client"].request(
        "POST" if route == "preset" else "PUT", _PREFIX + _A + "/" + route, headers=ctx["owner"], json=payload
    )
    _body(response)
    row = ctx["storage"].get_user(_A)
    _equal((row["source"], row["external_id"]), (before["source"], "origin-271"), "account_origin_preserved")


def _supervisor(ctx, target, supervisor):
    before, audit = _accounts(ctx), _audit(ctx["storage"], _ACTIONS[1])
    prior = next(r for r in before if r["id"] == target)
    metadata = json.loads(prior["metadata_json"])
    if supervisor:
        metadata["supervisor_id"] = supervisor
    else:
        metadata.pop("supervisor_id", None)
    body = _body(
        ctx["client"].post(
            _PREFIX + target + "/supervisor", headers=ctx["owner"], json={"supervisor_id": supervisor}
        )
    )
    row = ctx["storage"].get_user(target)
    _equal(json.loads(row["metadata_json"]), metadata, "account_supervisor_metadata")
    _equal(body, {"user": row, "supervisor_id": supervisor}, "account_supervisor_http")
    _preserved(ctx, before, target=target, changed={"metadata_json", "updated_at"})
    _audit_delta(ctx, _ACTIONS[1], audit, target=target)
    entry = _audit_values(ctx, _ACTIONS[1])
    _equal(entry.before_json, prior, "account_supervisor_audit_before")
    _equal(entry.after_json, row, "account_supervisor_audit_after")


def _assert_supervisor(ctx):
    url = _PREFIX + _A + "/activity"
    _equal(ctx["client"].get(url, headers=ctx[_ADMIN]).status_code, 200, "account_hierarchy_unconfigured")
    _supervisor(ctx, _B, LEGACY_OWNER_USER_ID)
    audit = _audit(ctx["storage"], "admin.user.activity.out_of_scope")
    _equal(ctx["client"].get(url, headers=ctx[_ADMIN]).status_code, 403, "account_hierarchy_stranger_refused")
    _audit_delta(ctx, "admin.user.activity.out_of_scope", audit, actor=_ADMIN)
    _supervisor(ctx, _A, _ADMIN)
    _equal(
        ctx["client"].get(url, headers=ctx[_ADMIN]).status_code, 200, "account_hierarchy_subordinate_allowed"
    )
    before, audit = _accounts(ctx), _audit(ctx["storage"], _ACTIONS[1])
    for target, supervisor, status in ((_ADMIN, _A, 400), (_A, _A, 400), (_A, "local:missing", 404)):
        response = ctx["client"].post(
            _PREFIX + target + "/supervisor", headers=ctx["owner"], json={"supervisor_id": supervisor}
        )
        _equal(response.status_code, status, "account_supervisor_invalid")
        _preserved(ctx, before)
        _equal(_audit(ctx["storage"], _ACTIONS[1]), audit, "account_supervisor_refusal_audit")
    _supervisor(ctx, _A, "")
    _equal(
        ctx["client"].get(url, headers=ctx[_ADMIN]).status_code, 403, "account_hierarchy_clear_removes_scope"
    )
    _equal(ctx["client"].get(url, headers=ctx["owner"]).status_code, 200, "account_hierarchy_owner")


def test_account_supervisor_http_sets_clears_and_enforces_hierarchy_with_cycle_refusals(account_http):
    _assert_supervisor(account_http)


def _seed_activity(ctx):
    for ident, author, day in (
        ("account-new", _A, "2024-02-03"),
        ("account-old", _A, "2024-02-01"),
        ("account-foreign", _B, "2024-02-02"),
        ("account-unattributed", None, "2024-02-02"),
        ("account-before-window", _A, "2024-01-31"),
        ("account-after-window", _A, "2024-02-05"),
    ):
        ctx["storage"].store_raw_object(
            RawObject(
                id=ident,
                user_id=LEGACY_OWNER_USER_ID,
                source="upload",
                source_ref="/private/" + ident,
                raw_content="PRIVATE-" + ident,
                content_type="file",
                content_hash=ident,
                metadata_json={"filename": ident + ".txt", **({"uploaded_by": author} if author else {})},
            )
        )
        with ctx["storage"].transaction() as connection:
            connection.execute(
                "UPDATE raw_objects SET received_at=? WHERE id=?", (day + "T12:00:00+00:00", ident)
            )


def _assert_activity(ctx):
    _seed_activity(ctx)
    before = _accounts(ctx)
    raw_before = [dict(r) for r in ctx["storage"].execute("SELECT * FROM raw_objects ORDER BY id").fetchall()]
    for actor, mode, grant in (
        (_ADMIN, "full", "admin.all_data.read"),
        (_WATCH, "redacted", "admin.activity.read"),
    ):
        for offset, wanted in ((0, "account-new"), (1, "account-old")):
            audit = _audit(ctx["storage"], "admin.user.activity.read")
            params = {"limit": 1, "offset": offset, "since": "2024-02-01", "until": "2024-02-04"}
            body = _body(ctx["client"].get(_PREFIX + _A + "/activity", params=params, headers=ctx[actor]))
            _equal((body["user_id"], body["content"]), (_A, mode), "account_activity_person_mode")
            _equal(body["summary"]["arrivals"], 2, "account_activity_author_count")
            _equal([r["raw_object_id"] for r in body["items"]], [wanted], "account_activity_author_page")
            item = body["items"][0]
            fields = {
                "preview": "PRIVATE-" + wanted,
                "filename": wanted + ".txt",
                "title": wanted + ".txt",
                "source_ref": "/private/" + wanted,
            }
            _equal(
                {k: item[k] for k in fields},
                fields if mode == "full" else dict.fromkeys(fields, ""),
                "account_activity_content_boundary",
            )
            if mode == "redacted":
                assert "PRIVATE-" not in json.dumps(body), "account_activity_no_private_fields"
                assert item["redacted"] is True, "account_activity_redaction_marker"
            row = _audit_delta(ctx, "admin.user.activity.read", audit, actor=actor)
            after = json.loads(row["after_json"])
            _equal((after["content"], after["granted_by"]), (mode, grant), "account_activity_audit_authority")
            _preserved(ctx, before)
            _equal(
                [dict(r) for r in ctx["storage"].execute("SELECT * FROM raw_objects ORDER BY id").fetchall()],
                raw_before,
                "account_activity_read_preserves_data",
            )


def test_account_activity_shared_tenant_filters_author_pages_content_and_personal_audit(account_http):
    _assert_activity(account_http)


def _assert_refusals(ctx):
    before, overrides = _accounts(ctx), _overrides(ctx)
    audit = {a: _audit(ctx["storage"], a) for a in _ACTIONS}
    routes = [
        ("GET", "resolve?name=Саша", None),
        ("GET", _A + "/activity", None),
        ("POST", _A + "/preset", {"preset_key": "admin"}),
        ("POST", _A + "/supervisor", {"supervisor_id": _ADMIN}),
        ("PUT", _A + "/permissions/" + _READ, {"effect": "allow"}),
    ]
    attempts = [
        (method, route, payload, headers, status)
        for headers, status in (({}, 401), (ctx[_B], 403))
        for method, route, payload in routes
    ]
    for method, suffix, payload in routes[2:]:
        attempts.append((method, suffix.replace(_A, LEGACY_OWNER_USER_ID), payload, ctx[_ADMIN], 403))
        attempts.append((method, suffix.replace(_A, "local:missing"), payload, ctx["owner"], 404))
    attempts += [
        ("POST", _A + "/preset", {"preset_key": "owner"}, ctx[_ADMIN], 403),
        ("PUT", _A + "/permissions/code.run", {"effect": "allow"}, ctx[_ADMIN], 403),
        ("POST", _A + "/preset", {"preset_key": "missing-preset"}, ctx["owner"], 400),
        ("PUT", _A + "/permissions/" + _READ, {"effect": "invalid"}, ctx["owner"], 400),
        ("GET", "resolve?name=", None, ctx["owner"], 422),
        ("GET", _A + "/activity?limit=0", None, ctx["owner"], 422),
    ]
    for method, route, payload, headers, status in attempts:
        response = ctx["client"].request(method, _PREFIX + route, headers=headers, json=payload)
        _equal(response.status_code, status, "account_refusal_status")
        _preserved(ctx, before)
        _equal(_overrides(ctx), overrides, "account_refusal_no_permission_effect")
        for action, prior in audit.items():
            _equal(_audit(ctx["storage"], action), prior, "account_refusal_no_success_audit")


def test_account_http_authority_validation_and_missing_target_refusals_have_no_account_effect(account_http):
    _assert_refusals(account_http)


@pytest.mark.parametrize(
    "fault,oracle,code",
    [
        ("resolve_winner", _assert_resolve, "account_resolve_ambiguity"),
        ("resolve_private", _assert_resolve, "account_resolve_safe_exact_match"),
        ("preset_unwritten", _assert_preset, "account_preset_persisted"),
        ("preset_collateral", _assert_preset, "account_other_rows_preserved"),
        ("preset_auth", _assert_preset, "account_preset_effective_http"),
        ("permission_unwritten", _assert_permission, "account_permission_persisted"),
        ("permission_auth", _assert_permission, "account_permission_effective_http"),
        ("supervisor_metadata", _assert_supervisor, "account_supervisor_metadata"),
        ("supervisor_scope", _assert_supervisor, "account_hierarchy_stranger_refused"),
        ("activity_author", _assert_activity, "account_activity_author_page"),
        ("activity_private", _assert_activity, "account_activity_content_boundary"),
        ("activity_audit", _assert_activity, "account_audit_count"),
        ("refusal_effect", _assert_refusals, "account_other_rows_preserved"),
    ],
    ids=[
        "resolve_winner",
        "resolve_private",
        "preset_unwritten",
        "preset_collateral",
        "preset_auth",
        "permission_unwritten",
        "permission_auth",
        "supervisor_metadata",
        "supervisor_scope",
        "activity_author",
        "activity_private",
        "activity_audit",
        "refusal_effect",
    ],
)
def test_account_oracles_detect_real_http_and_persisted_faults(
    account_http, monkeypatch, fault, oracle, code
):
    ctx = account_http
    original = ctx["client"].request
    log = ctx["storage"].log_audit

    def audit(entry):
        if fault == "activity_audit" and entry.action == "admin.user.activity.read":
            return entry
        return log(entry)

    def request(method, url, **kwargs):
        response = original(method, url, **kwargs)
        path = str(url).split("?", 1)[0]
        body = response.json()
        if response.status_code == 200:
            if path.endswith("/resolve") and fault == "resolve_winner" and body["query"] == "Саша":
                body["unambiguous"] = body["matches"][0]
            if path.endswith("/resolve") and fault == "resolve_private":
                body["matches"][0]["private_note"] = "PRIVATE-account"
            if path.endswith("/preset"):
                if fault == "preset_unwritten":
                    ctx["storage"].update_user(_A, preset_key="user")
                if fault == "preset_collateral":
                    ctx["storage"].update_user(_B, preset_key="admin")
            if "/permissions/" in path and fault == "permission_unwritten":
                ctx["storage"].set_permission_override(_A, _READ, None)
            if path.endswith("/supervisor") and fault == "supervisor_metadata":
                ctx["storage"].update_user(_B, metadata_json={"supervisor_id": LEGACY_OWNER_USER_ID})
            if path.endswith("/activity") and fault == "activity_author":
                body["items"][0]["raw_object_id"] = "account-foreign"
            if path.endswith("/activity") and fault == "activity_private" and body["content"] == "redacted":
                body["items"][0]["preview"] = "PRIVATE-account-new"
        if (
            path.endswith("/resolve")
            and fault in {"preset_auth", "permission_auth"}
            and kwargs.get("headers") == ctx[_A]
        ):
            return httpx.Response(
                200 if response.status_code == 403 else 403, json=body, request=response.request
            )
        if path.endswith("/activity") and fault == "supervisor_scope" and response.status_code == 403:
            return httpx.Response(200, json=body, request=response.request)
        if fault == "refusal_effect" and response.status_code == 403:
            ctx["storage"].update_user(_A, preset_key="admin")
        return httpx.Response(response.status_code, json=body, request=response.request)

    monkeypatch.setattr(ctx["storage"], "log_audit", audit)
    monkeypatch.setattr(ctx["client"], "request", request)
    with pytest.raises(AssertionError, match=code):
        oracle(ctx)
