"""Capability and custom-preset HTTP contracts for the frozen R10 candidate."""

from __future__ import annotations

import copy
import json
import re
from dataclasses import replace
from datetime import UTC, datetime

import httpx
import pytest
from fastapi.testclient import TestClient

from friday.permissions import LEGACY_OWNER_USER_ID
from friday.server import create_app
from tests.test_api_tokens import _issue
from tests.test_release_1_0_conversation_oracles import _body, _equal

_ADMIN = "local:r10-preset-admin"
_ORDINARY = "local:r10-preset-ordinary"
_ASSIGNED = "local:r10-preset-assigned"
_SIBLING = "local:r10-preset-sibling"
_FIXTURE = "local:r10-preset-fixture"

_ASSIGNED_PRESET = "r10_assigned_reader"
_DELETABLE_PRESET = "r10_delete_me"
_OTHER_PRESET = "r10_other_chat"
_NEW_PRESET = "r10_private_create"
_DELEGATED_PRESET = "r10_delegated_create"

_EXPECTED_CAPABILITY_IDS = (
    "admin.activity.read",
    "admin.all_data.manage",
    "admin.all_data.read",
    "admin.audit.read",
    "admin.backup.manage",
    "admin.data.purge",
    "admin.diagnostics",
    "admin.export",
    "admin.missions.manage",
    "admin.missions.read",
    "admin.presets.manage",
    "admin.tokens.manage",
    "admin.users.manage",
    "admin.users.read",
    "chat.use",
    "chronicle.read",
    "code.run",
    "compact.read",
    "compact.run",
    "conversations.manage",
    "conversations.read",
    "data.read",
    "feedback.write",
    "files.read",
    "files.upload",
    "import.run",
    "inbox.read",
    "inbox.review",
    "kg.merge",
    "kg.read",
    "kg.write",
    "knowledge.create",
    "knowledge.delete",
    "knowledge.edit",
    "knowledge.read",
    "mcp.files.create",
    "mcp.files.read",
    "missions.control",
    "missions.create",
    "missions.read",
    "profile.read",
    "reflection.read",
    "search.use",
    "tts.use",
    "web.compare.transient",
    "web.fetch",
    "web.research",
    "web.search",
)

_MODERATOR_CAPABILITIES = (
    "chat.use",
    "chronicle.read",
    "compact.read",
    "conversations.manage",
    "conversations.read",
    "feedback.write",
    "files.read",
    "files.upload",
    "import.run",
    "inbox.read",
    "inbox.review",
    "kg.merge",
    "kg.read",
    "kg.write",
    "knowledge.create",
    "knowledge.delete",
    "knowledge.edit",
    "knowledge.read",
    "missions.control",
    "missions.create",
    "missions.read",
    "profile.read",
    "reflection.read",
    "search.use",
    "tts.use",
    "web.fetch",
    "web.research",
    "web.search",
)

_USER_CAPABILITIES = (
    "chat.use",
    "chronicle.read",
    "compact.read",
    "conversations.manage",
    "conversations.read",
    "feedback.write",
    "files.read",
    "files.upload",
    "import.run",
    "inbox.read",
    "inbox.review",
    "kg.read",
    "kg.write",
    "knowledge.create",
    "knowledge.delete",
    "knowledge.edit",
    "knowledge.read",
    "missions.control",
    "missions.create",
    "missions.read",
    "profile.read",
    "reflection.read",
    "search.use",
    "tts.use",
    "web.fetch",
    "web.search",
)

_GUEST_CAPABILITIES = (
    "chat.use",
    "conversations.read",
    "feedback.write",
    "kg.read",
    "knowledge.read",
    "search.use",
    "tts.use",
)

_CAPABILITY_ANCHORS = {
    "admin.presets.manage": {
        "security_id": "admin.presets.manage",
        "description": "Manage custom permission presets",
        "category": "admin",
        "risk_level": 3,
        "default_presets": ["admin"],
        "default_requires_hitl": False,
        "source": "core",
    },
    "chat.use": {
        "security_id": "chat.use",
        "description": "Use the conversational agent",
        "category": "chat",
        "risk_level": 0,
        "default_presets": ["admin", "moderator", "user", "guest"],
        "default_requires_hitl": False,
        "source": "core",
    },
    "chronicle.read": {
        "security_id": "chronicle.read",
        "description": "Read the episodic timeline of recent and past-on-this-day knowledge",
        "category": "chronicle",
        "risk_level": 0,
        "default_presets": ["admin", "moderator", "user"],
        "default_requires_hitl": False,
        "source": "organ",
    },
    "code.run": {
        "security_id": "code.run",
        "description": "Run code in the restricted subprocess executor",
        "category": "execution",
        "risk_level": 4,
        "default_presets": [],
        "default_requires_hitl": False,
        "source": "core",
    },
}


@pytest.fixture
def preset_http(settings):
    assert not settings.llm_enabled and not settings.workers_enabled
    app = create_app(replace(settings, shared_archive=True, telegram_owner_chat_ids=[]))
    with TestClient(app) as client:
        storage = app.state.storage
        storage.ensure_user(_FIXTURE, source="local", preset_key="user")
        storage.upsert_custom_preset(
            _ASSIGNED_PRESET,
            "Assigned private reader",
            {"admin.users.read"},
            description="Private assigned policy",
            created_by=_FIXTURE,
        )
        storage.upsert_custom_preset(
            _DELETABLE_PRESET,
            "Deletable private preset",
            {"knowledge.read"},
            description="Private deletion canary",
            created_by=_FIXTURE,
        )
        storage.upsert_custom_preset(
            _OTHER_PRESET,
            "Other private preset",
            {"chat.use", "knowledge.read"},
            description="Private sibling policy",
            created_by=_FIXTURE,
        )
        headers = {}
        for key, user_id, preset_key in (
            ("admin", _ADMIN, "admin"),
            ("ordinary", _ORDINARY, "user"),
            ("assigned", _ASSIGNED, _ASSIGNED_PRESET),
        ):
            secret = f"jrc_synthetic_r10_preset_{key}_" + "7" * 16
            _issue(storage, user_id, preset_key, secret)
            headers[key] = {"Authorization": f"Bearer {secret}"}
        storage.ensure_user(_SIBLING, source="local", preset_key="user")
        storage.set_permission_override(_SIBLING, "admin.presets.manage", "deny")
        storage.link_identity("email", "preset-sibling@example.invalid", _SIBLING, linked_by=_FIXTURE)
        yield {
            "app": app,
            "client": client,
            "storage": storage,
            "owner": {"Authorization": f"Bearer {settings.api_token}"},
            "headers": headers,
        }


def _custom_rows(ctx):
    return copy.deepcopy(ctx["storage"].list_custom_presets())


def _capability_rows(ctx):
    return [
        tuple(row)
        for row in ctx["storage"]
        .execute("SELECT preset_key, security_id FROM preset_capabilities ORDER BY preset_key, security_id")
        .fetchall()
    ]


def _account_rows(ctx):
    rows = [dict(row) for row in ctx["storage"].execute("SELECT * FROM users ORDER BY id").fetchall()]
    for row in rows:
        if row["id"] == LEGACY_OWNER_USER_ID:
            # Configured-owner authentication updates these two activity times.
            # Creation time and all authority/metadata fields remain exact.
            row.pop("updated_at")
            row.pop("last_seen_at")
    return rows


def _policy_rows(ctx):
    return {
        table: [
            dict(row) for row in ctx["storage"].execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
        ]
        for table in ("user_permission_overrides", "user_identities")
    }


def _assert_capability_write(ctx, before, key, capabilities, code):
    wanted = sorted([row for row in before if row[0] != key] + [(key, cap) for cap in capabilities])
    _equal(_capability_rows(ctx), wanted, code)


def _assert_creation_time(row, started):
    stamp = datetime.fromisoformat(row["created_at"])
    assert stamp.tzinfo is not None and started <= stamp <= datetime.now(UTC), (
        "preset_create_timestamp_bounds"
    )


def _preset_audits(ctx):
    return [
        dict(row)
        for row in ctx["storage"]
        .execute(
            """SELECT rowid, * FROM audit_log
               WHERE target_type='preset' OR action LIKE 'admin.preset.%'
               ORDER BY rowid"""
        )
        .fetchall()
    ]


def _state(ctx):
    return {
        "custom": _custom_rows(ctx),
        "capabilities": _capability_rows(ctx),
        "accounts": _account_rows(ctx),
        "policy": _policy_rows(ctx),
        "audits": _preset_audits(ctx),
    }


def _json_or_none(value):
    return json.loads(value) if value is not None else None


def _privacy_projection(preset):
    if preset is None:
        return None
    return {
        "name_chars": len(preset["name"]),
        "description_chars": len(preset["description"]),
        "created_at": preset["created_at"],
        "updated_at": preset["updated_at"],
        "private_fields_count": 3,
        "private_chars": len(preset["preset_key"]) + len(preset["created_by"]),
        "private_items_count": len(preset["capabilities"]),
    }


def _capture_raw_audits(ctx, monkeypatch):
    captured = []
    original = ctx["storage"].log_audit

    def capture(entry):
        captured.append(copy.deepcopy(entry))
        return original(entry)

    monkeypatch.setattr(ctx["storage"], "log_audit", capture)
    return captured


def _assert_raw_audit(entry, *, actor, action, key, before, after, code):
    _equal(
        (entry.user_id, entry.action, entry.target_type, entry.target_id),
        (actor, action, "preset", key),
        code + "_attribution",
    )
    _equal(entry.before_json, before, code + "_before")
    _equal(entry.after_json, after, code + "_after")


def _assert_persisted_audit(
    ctx,
    before_rows,
    *,
    actor,
    action,
    key,
    before,
    after,
    code,
):
    rows = _preset_audits(ctx)
    _equal(rows[: len(before_rows)], before_rows, code + "_history")
    added = rows[len(before_rows) :]
    _equal(len(added), 1, code + "_count")
    row = added[0]
    _equal((row["user_id"], row["action"], row["target_type"]), (actor, action, "preset"), code)
    assert re.fullmatch(r"preset:ref:[0-9a-f]{24}", str(row["target_id"])), code + "_opaque_target"
    _equal(_json_or_none(row["before_json"]), _privacy_projection(before), code + "_before")
    _equal(_json_or_none(row["after_json"]), _privacy_projection(after), code + "_after")
    durable = json.dumps(
        {"target": row["target_id"], "before": row["before_json"], "after": row["after_json"]},
        ensure_ascii=False,
    )
    for private in (
        key,
        *(() if before is None else (before["name"], before["description"], before["created_by"])),
        *(() if after is None else (after["name"], after["description"], after["created_by"])),
    ):
        assert private not in durable, code + "_private_projection"
    return row


def _assert_capability_catalog(ctx):
    before = _state(ctx)
    body = _body(ctx["client"].get("/api/admin/capabilities", headers=ctx["owner"]))
    _equal(set(body), {"items", "count"}, "capability_response_shape")
    _equal(body["count"], 48, "capability_count")
    items = body["items"]
    _equal(len(items), 48, "capability_count")
    ids = [item["security_id"] for item in items]
    _equal(ids, list(_EXPECTED_CAPABILITY_IDS), "capability_ids")
    _equal(len(ids), len(set(ids)), "capability_unique")
    for item in items:
        _equal(
            set(item),
            {
                "security_id",
                "description",
                "category",
                "risk_level",
                "default_presets",
                "default_requires_hitl",
                "source",
            },
            "capability_item_shape",
        )
        assert isinstance(item["description"], str) and item["description"].strip(), "capability_description"
        assert isinstance(item["category"], str) and re.fullmatch(r"[a-z][a-z0-9_.-]*", item["category"]), (
            "capability_category"
        )
        assert type(item["risk_level"]) is int and 0 <= item["risk_level"] <= 5, "capability_risk"
        assert item["source"] in {"core", "organ"}, "capability_source"
        assert type(item["default_requires_hitl"]) is bool, "capability_hitl"
        assert isinstance(item["default_presets"], list), "capability_defaults_type"
        assert set(item["default_presets"]) <= {"owner", "admin", "moderator", "user", "guest"}, (
            "capability_defaults"
        )
    by_id = {item["security_id"]: item for item in items}
    for security_id, expected in _CAPABILITY_ANCHORS.items():
        _equal(by_id[security_id], expected, "capability_anchor")
    _equal(_state(ctx), before, "capability_get_no_preset_effect")


def _expected_builtin_capabilities():
    all_ids = set(_EXPECTED_CAPABILITY_IDS)
    return {
        "owner": list(_EXPECTED_CAPABILITY_IDS),
        "admin": sorted(all_ids - {"code.run", "web.compare.transient"}),
        "moderator": list(_MODERATOR_CAPABILITIES),
        "user": list(_USER_CAPABILITIES),
        "guest": list(_GUEST_CAPABILITIES),
    }


def _assert_preset_catalog(ctx):
    before = _state(ctx)
    body = _body(ctx["client"].get("/api/admin/presets", headers=ctx["owner"]))
    _equal(set(body), {"items", "count"}, "preset_response_shape")
    _equal(body["count"], 8, "preset_count")
    _equal(len(body["items"]), 8, "preset_count")
    builtins = body["items"][:5]
    _equal(
        [row["preset_key"] for row in builtins],
        ["owner", "admin", "moderator", "user", "guest"],
        "preset_builtin_keys",
    )
    expected_caps = _expected_builtin_capabilities()
    for row in builtins:
        key = row["preset_key"]
        _equal(
            row,
            {
                "preset_key": key,
                "name": key.title(),
                "built_in": True,
                "capabilities": expected_caps[key],
            },
            "preset_builtin_membership",
        )
    expected_custom = [{**row, "built_in": False} for row in _custom_rows(ctx)]
    _equal(body["items"][5:], expected_custom, "preset_custom_projection")
    _equal(
        [row["preset_key"] for row in expected_custom],
        sorted(row["preset_key"] for row in expected_custom),
        "preset_custom_order",
    )
    _equal(_state(ctx), before, "preset_get_no_effect")


def _assert_owner_create(ctx, monkeypatch):
    before_custom = _custom_rows(ctx)
    before_accounts = _account_rows(ctx)
    before_policy = _policy_rows(ctx)
    before_capabilities = _capability_rows(ctx)
    before_audits = _preset_audits(ctx)
    captured = _capture_raw_audits(ctx, monkeypatch)
    started = datetime.now(UTC).replace(microsecond=0)
    name = "Private create name 031"
    description = "/private/r10/preset/create-canary"
    response = ctx["client"].post(
        "/api/admin/presets",
        headers=ctx["owner"],
        json={
            "preset_key": _NEW_PRESET,
            "name": name,
            "description": description,
            "capabilities": ["chat.use", "admin.users.read", "chat.use"],
        },
    )
    body = _body(response)
    row = ctx["storage"].get_custom_preset(_NEW_PRESET)
    assert row is not None, "preset_create_persisted"
    _equal(
        set(row),
        {"preset_key", "name", "description", "created_by", "created_at", "updated_at", "capabilities"},
        "preset_create_shape",
    )
    _equal(
        {key: row[key] for key in ("preset_key", "name", "description", "created_by", "capabilities")},
        {
            "preset_key": _NEW_PRESET,
            "name": name,
            "description": description,
            "created_by": LEGACY_OWNER_USER_ID,
            "capabilities": ["admin.users.read", "chat.use"],
        },
        "preset_create_fields",
    )
    _equal(row["created_at"], row["updated_at"], "preset_create_timestamps")
    _assert_creation_time(row, started)
    _equal(body, {"preset": row}, "preset_create_http_storage")
    _equal(
        [item for item in _custom_rows(ctx) if item["preset_key"] != _NEW_PRESET],
        before_custom,
        "preset_create_preserves_other_presets",
    )
    _equal(_account_rows(ctx), before_accounts, "preset_create_preserves_accounts")
    _equal(_policy_rows(ctx), before_policy, "preset_create_preserves_policy")
    _assert_capability_write(
        ctx,
        before_capabilities,
        _NEW_PRESET,
        ["admin.users.read", "chat.use"],
        "preset_create_capability_rows",
    )
    _equal(len(captured), 1, "preset_create_audit_count")
    _assert_raw_audit(
        captured[0],
        actor=LEGACY_OWNER_USER_ID,
        action="admin.preset.upsert",
        key=_NEW_PRESET,
        before=None,
        after=row,
        code="preset_create_raw_audit",
    )
    _assert_persisted_audit(
        ctx,
        before_audits,
        actor=LEGACY_OWNER_USER_ID,
        action="admin.preset.upsert",
        key=_NEW_PRESET,
        before=None,
        after=row,
        code="preset_create_audit",
    )


def _assert_assigned_update(ctx, monkeypatch):
    client, storage = ctx["client"], ctx["storage"]
    granted = client.get("/api/admin/capabilities", headers=ctx["headers"]["assigned"])
    _equal(granted.status_code, 200, "preset_update_initial_personal_authority")
    before = copy.deepcopy(storage.get_custom_preset(_ASSIGNED_PRESET))
    assert before is not None
    before_custom = _custom_rows(ctx)
    before_accounts = _account_rows(ctx)
    before_policy = _policy_rows(ctx)
    before_capabilities = _capability_rows(ctx)
    before_audits = _preset_audits(ctx)
    captured = _capture_raw_audits(ctx, monkeypatch)
    from friday.storage import _accounts as account_store

    updated_at = "2042-03-17T12:34:56+00:00"
    monkeypatch.setattr(account_store, "utc_now", lambda: updated_at)
    response = client.post(
        "/api/admin/presets",
        headers=ctx["owner"],
        json={
            "preset_key": _ASSIGNED_PRESET,
            "name": "Updated assigned private reader",
            "description": "Updated private assigned policy",
            "capabilities": ["chat.use"],
        },
    )
    body = _body(response)
    after = storage.get_custom_preset(_ASSIGNED_PRESET)
    assert after is not None
    _equal(body, {"preset": after}, "preset_update_http_storage")
    _equal(
        {
            "preset_key": after["preset_key"],
            "name": after["name"],
            "description": after["description"],
            "created_by": after["created_by"],
            "created_at": after["created_at"],
            "capabilities": after["capabilities"],
        },
        {
            "preset_key": _ASSIGNED_PRESET,
            "name": "Updated assigned private reader",
            "description": "Updated private assigned policy",
            "created_by": before["created_by"],
            "created_at": before["created_at"],
            "capabilities": ["chat.use"],
        },
        "preset_update_fields",
    )
    _equal(after["updated_at"], updated_at, "preset_update_timestamp")
    assert after["updated_at"] != before["updated_at"], "preset_update_timestamp_changed"
    _equal(
        [row for row in _custom_rows(ctx) if row["preset_key"] != _ASSIGNED_PRESET],
        [row for row in before_custom if row["preset_key"] != _ASSIGNED_PRESET],
        "preset_update_preserves_other_presets",
    )
    _equal(_account_rows(ctx), before_accounts, "preset_update_preserves_accounts")
    _equal(_policy_rows(ctx), before_policy, "preset_update_preserves_policy")
    _assert_capability_write(
        ctx, before_capabilities, _ASSIGNED_PRESET, ["chat.use"], "preset_update_capability_rows"
    )
    _equal(storage.get_user(_ASSIGNED)["preset_key"], _ASSIGNED_PRESET, "preset_update_preserves_assignment")
    refused = client.get("/api/admin/capabilities", headers=ctx["headers"]["assigned"])
    _equal(refused.status_code, 403, "preset_update_subsequent_personal_authority")
    catalog = _body(client.get("/api/admin/presets", headers=ctx["owner"]))
    reflected = next(row for row in catalog["items"] if row["preset_key"] == _ASSIGNED_PRESET)
    _equal(reflected, {**after, "built_in": False}, "preset_update_catalog_reflection")
    _equal(len(captured), 1, "preset_update_audit_count")
    _assert_raw_audit(
        captured[0],
        actor=LEGACY_OWNER_USER_ID,
        action="admin.preset.upsert",
        key=_ASSIGNED_PRESET,
        before=before,
        after=after,
        code="preset_update_raw_audit",
    )
    _assert_persisted_audit(
        ctx,
        before_audits,
        actor=LEGACY_OWNER_USER_ID,
        action="admin.preset.upsert",
        key=_ASSIGNED_PRESET,
        before=before,
        after=after,
        code="preset_update_audit",
    )


def _assert_update_authority(ctx):
    client, storage = ctx["client"], ctx["storage"]
    _equal(
        client.get("/api/admin/capabilities", headers=ctx["headers"]["assigned"]).status_code,
        200,
        "preset_update_initial_personal_authority",
    )
    response = client.post(
        "/api/admin/presets",
        headers=ctx["owner"],
        json={
            "preset_key": _ASSIGNED_PRESET,
            "name": "Authority refresh private preset",
            "description": "Authority refresh private policy",
            "capabilities": ["chat.use"],
        },
    )
    _equal(response.status_code, 200, "preset_update_authority_write")
    _equal(
        storage.get_custom_preset(_ASSIGNED_PRESET)["capabilities"],
        ["chat.use"],
        "preset_update_authority_persisted",
    )
    _equal(
        storage.get_user(_ASSIGNED)["preset_key"],
        _ASSIGNED_PRESET,
        "preset_update_authority_assignment",
    )
    _equal(
        client.get("/api/admin/capabilities", headers=ctx["headers"]["assigned"]).status_code,
        403,
        "preset_update_subsequent_personal_authority",
    )


def _assert_invalid_post_refusals(ctx):
    before = _state(ctx)
    requests = (
        {},
        {"capabilities": []},
        {"preset_key": "BAD_KEY", "name": "private", "capabilities": []},
        {"preset_key": "guest", "name": "private", "capabilities": []},
        {"preset_key": "owner", "name": "private", "capabilities": []},
        {"preset_key": "r10_bad_caps", "name": "private", "capabilities": "chat.use"},
        {"preset_key": "r10_unknown_cap", "name": "private", "capabilities": ["private.unknown"]},
    )
    statuses = [
        ctx["client"].post("/api/admin/presets", headers=ctx["owner"], json=payload).status_code
        for payload in requests
    ]
    malformed = ctx["client"].post(
        "/api/admin/presets",
        headers={**ctx["owner"], "content-type": "application/json"},
        content=b"{",
    )
    _equal(statuses + [malformed.status_code], [400] * 8, "preset_post_invalid_statuses")
    _equal(_state(ctx), before, "preset_post_refusal_no_effect")


def test_capability_catalog_has_exact_membership_safe_metadata_and_no_effect(preset_http):
    _assert_capability_catalog(preset_http)


def test_preset_catalog_has_exact_builtin_membership_and_persisted_custom_rows(preset_http):
    _assert_preset_catalog(preset_http)


def test_owner_create_deduplicates_persists_preserves_and_audits_private_fields(preset_http, monkeypatch):
    _assert_owner_create(preset_http, monkeypatch)


def test_assigned_preset_update_changes_subsequent_personal_http_authority_and_audits_before_after(
    preset_http, monkeypatch
):
    _assert_assigned_update(preset_http, monkeypatch)


def test_shared_archive_delegated_create_is_attributed_to_the_acting_person(preset_http, monkeypatch):
    ctx = preset_http
    before_custom = _custom_rows(ctx)
    before_accounts = _account_rows(ctx)
    before_policy = _policy_rows(ctx)
    before_capabilities = _capability_rows(ctx)
    before_audits = _preset_audits(ctx)
    captured = _capture_raw_audits(ctx, monkeypatch)
    body = _body(
        ctx["client"].post(
            "/api/admin/presets",
            headers=ctx["headers"]["admin"],
            json={
                "preset_key": _DELEGATED_PRESET,
                "name": "Delegated private preset",
                "description": "Private delegated description",
                "capabilities": ["admin.users.read"],
            },
        )
    )
    row = ctx["storage"].get_custom_preset(_DELEGATED_PRESET)
    assert row is not None
    _equal(body, {"preset": row}, "preset_delegated_http_storage")
    _equal(
        [item for item in _custom_rows(ctx) if item["preset_key"] != _DELEGATED_PRESET],
        before_custom,
        "preset_delegated_preserves_others",
    )
    _equal(_account_rows(ctx), before_accounts, "preset_delegated_preserves_accounts")
    _equal(_policy_rows(ctx), before_policy, "preset_delegated_preserves_policy")
    _assert_capability_write(
        ctx, before_capabilities, _DELEGATED_PRESET, ["admin.users.read"], "preset_delegated_capability_rows"
    )
    _equal(len(captured), 1, "preset_delegated_audit_count")
    _equal(
        (captured[0].user_id, _preset_audits(ctx)[-1]["user_id"]),
        (_ADMIN, _ADMIN),
        "preset_delegated_audit_actor",
    )
    _assert_persisted_audit(
        ctx,
        before_audits,
        actor=_ADMIN,
        action="admin.preset.upsert",
        key=_DELEGATED_PRESET,
        before=None,
        after=row,
        code="preset_delegated_audit",
    )
    _equal(
        (row["created_by"], captured[0].after_json["created_by"]),
        (_ADMIN, _ADMIN),
        "preset_delegated_person_attribution",
    )


def test_delegated_admin_cannot_rewrite_a_custom_preset_assigned_to_owner(preset_http):
    ctx = preset_http
    control_key = "r10_unassigned_admin_update"
    ctx["storage"].upsert_custom_preset(
        control_key,
        "Unassigned admin policy",
        {"admin.users.read", "chat.use"},
        description="Private unassigned policy",
        created_by=_FIXTURE,
    )
    control = ctx["client"].post(
        "/api/admin/presets",
        headers=ctx["headers"]["admin"],
        json={
            "preset_key": control_key,
            "name": "Unassigned admin policy updated",
            "description": "Private unassigned policy updated",
            "capabilities": ["admin.users.read"],
        },
    )
    _equal(control.status_code, 200, "preset_unassigned_delegated_update_control")
    _equal(
        ctx["storage"].get_custom_preset(control_key)["capabilities"],
        ["admin.users.read"],
        "preset_unassigned_delegated_update_persisted",
    )
    key = "r10_owner_bound"
    ctx["storage"].upsert_custom_preset(
        key,
        "Owner private policy",
        {"admin.users.read", "chat.use"},
        description="Owner-bound private policy",
        created_by=LEGACY_OWNER_USER_ID,
    )
    ctx["storage"].update_user(LEGACY_OWNER_USER_ID, preset_key=key)
    before = _state(ctx)
    response = ctx["client"].post(
        "/api/admin/presets",
        headers=ctx["headers"]["admin"],
        json={
            "preset_key": key,
            "name": "Stripped owner policy",
            "description": "A delegated actor must not change this",
            "capabilities": ["admin.users.read"],
        },
    )
    _equal(
        (response.status_code, _state(ctx)),
        (403, before),
        "preset_owner_bound_update_refusal_no_effect",
    )


def test_preset_assignment_guard_and_upsert_share_one_transaction(preset_http, monkeypatch):
    ctx = preset_http
    storage = ctx["storage"]
    key = "r10_atomic_preset_update"
    storage.upsert_custom_preset(
        key,
        "Atomic preset",
        {"admin.users.read", "chat.use"},
        created_by=_FIXTURE,
    )
    storage.update_user(_SIBLING, preset_key=key)
    from friday.admin_api import _users as user_routes

    observed = []
    original_protect = user_routes._protect_owner_target
    original_upsert = storage.upsert_custom_preset

    def protect(request, user_id):
        if user_id == _SIBLING:
            observed.append(("guard", storage.conn.in_transaction))
        return original_protect(request, user_id)

    def upsert(*args, **kwargs):
        observed.append(("write", storage.conn.in_transaction))
        return original_upsert(*args, **kwargs)

    monkeypatch.setattr(user_routes, "_protect_owner_target", protect)
    monkeypatch.setattr(storage, "upsert_custom_preset", upsert)
    response = ctx["client"].post(
        "/api/admin/presets",
        headers=ctx["headers"]["admin"],
        json={
            "preset_key": key,
            "name": "Atomic preset updated",
            "description": "Guard and write use one boundary",
            "capabilities": ["admin.users.read"],
        },
    )
    _equal(response.status_code, 200, "preset_atomic_update_status")
    _equal(observed, [("guard", True), ("write", True)], "preset_atomic_guard_write")


def test_missing_invalid_and_builtin_preset_posts_are_refused_without_effect(preset_http):
    _assert_invalid_post_refusals(preset_http)


def test_anonymous_and_ordinary_accounts_cannot_read_or_mutate_preset_surfaces(preset_http):
    ctx = preset_http
    before = _state(ctx)
    calls = (
        ("get", "/api/admin/capabilities", None),
        ("get", "/api/admin/presets", None),
        (
            "post",
            "/api/admin/presets",
            {"preset_key": "r10_unauthorized", "name": "private", "capabilities": ["chat.use"]},
        ),
    )
    responses = []
    for headers in ({}, ctx["headers"]["ordinary"]):
        for method, path, payload in calls:
            responses.append(ctx["client"].request(method, path, headers=headers, json=payload))
    observed = [response.status_code for response in responses]
    _equal(observed, [401] * 3 + [403] * 3, "preset_surface_auth_refusals")
    for response in responses:
        assert _ASSIGNED_PRESET not in response.text, "preset_surface_auth_private_key"
        assert "Private assigned policy" not in response.text, "preset_surface_auth_private_metadata"
    _equal(_state(ctx), before, "preset_surface_auth_no_effect")


def test_delegated_admin_cannot_create_or_update_beyond_personal_authority(preset_http):
    ctx = preset_http
    unsafe_key = "r10_delegated_unsafe"
    ctx["storage"].upsert_custom_preset(
        unsafe_key,
        "Initially safe delegated preset",
        {"chat.use"},
        description="Private safe starting point",
        created_by=_FIXTURE,
    )
    before = _state(ctx)
    create = ctx["client"].post(
        "/api/admin/presets",
        headers=ctx["headers"]["admin"],
        json={
            "preset_key": "r10_escalation_create",
            "name": "Escalation create",
            "capabilities": ["code.run"],
        },
    )
    update = ctx["client"].post(
        "/api/admin/presets",
        headers=ctx["headers"]["admin"],
        json={
            "preset_key": unsafe_key,
            "name": "Escalation update",
            "capabilities": ["chat.use", "code.run"],
        },
    )
    _equal(
        (create.status_code, update.status_code, _state(ctx)),
        (403, 403, before),
        "preset_delegated_escalation_refusal_no_effect",
    )


def _install_fault(ctx, monkeypatch, fault):
    auth = ctx["app"].state.auth_service
    storage = ctx["storage"]
    if fault in {
        "create_override",
        "refusal_override",
        "create_orphan_capability",
        "create_account_creation",
    }:
        original = ctx["client"].post

        def damaged_post(*args, **kwargs):
            response = original(*args, **kwargs)
            with storage.transaction() as conn:
                if fault.endswith("override"):
                    conn.execute(
                        "UPDATE user_permission_overrides SET effect='allow' WHERE user_id=? AND security_id=?",
                        (_SIBLING, "admin.presets.manage"),
                    )
                elif fault == "create_orphan_capability":
                    conn.execute(
                        "INSERT INTO preset_capabilities(preset_key,security_id) VALUES(?,?)",
                        ("r10_orphan_policy", "code.run"),
                    )
                else:
                    conn.execute(
                        "UPDATE users SET created_at=? WHERE id=?", ("2042-01-01T00:00:00+00:00", _SIBLING)
                    )
            return response

        monkeypatch.setattr(ctx["client"], "post", damaged_post)
    elif fault == "capability_drop":
        original = auth.list_capabilities
        monkeypatch.setattr(auth, "list_capabilities", lambda: original()[:-1])
    elif fault == "capability_metadata":
        original = auth.list_capabilities

        def damaged_capabilities():
            return [
                replace(item, risk_level=0) if item.security_id == "admin.presets.manage" else item
                for item in original()
            ]

        monkeypatch.setattr(auth, "list_capabilities", damaged_capabilities)
    elif fault == "preset_membership":
        original = auth.list_presets

        def damaged_presets():
            rows = copy.deepcopy(original())
            admin = next(row for row in rows if row["preset_key"] == "admin")
            admin["capabilities"].remove("admin.presets.manage")
            return rows

        monkeypatch.setattr(auth, "list_presets", damaged_presets)
    elif fault == "update_stale_authority":
        original = auth._preset_grants

        def stale_preset_grants(preset_key):
            if preset_key == _ASSIGNED_PRESET:
                return {"admin.users.read"}
            return original(preset_key)

        monkeypatch.setattr(auth, "_preset_grants", stale_preset_grants)
    elif fault in {
        "create_unwritten",
        "create_wrong_capabilities",
        "create_collateral_preset",
        "create_collateral_account",
        "create_response",
    }:
        original = storage.upsert_custom_preset
        if fault == "create_unwritten":

            def damaged_writer(preset_key, name, capabilities, *, description="", created_by):
                stamp = "2024-01-01T00:00:00+00:00"
                return {
                    "preset_key": preset_key,
                    "name": name,
                    "description": description,
                    "created_by": created_by,
                    "created_at": stamp,
                    "updated_at": stamp,
                    "capabilities": sorted(capabilities),
                }

        elif fault == "create_wrong_capabilities":

            def damaged_writer(preset_key, name, capabilities, *, description="", created_by):
                return original(
                    preset_key,
                    name,
                    {"knowledge.read"},
                    description=description,
                    created_by=created_by,
                )

        elif fault == "create_collateral_preset":

            def damaged_writer(preset_key, name, capabilities, *, description="", created_by):
                row = original(preset_key, name, capabilities, description=description, created_by=created_by)
                original(
                    "r10_collateral_private",
                    "Collateral private preset",
                    {"chat.use"},
                    description="Unexpected private collateral",
                    created_by=_FIXTURE,
                )
                return row

        elif fault == "create_collateral_account":

            def damaged_writer(preset_key, name, capabilities, *, description="", created_by):
                row = original(preset_key, name, capabilities, description=description, created_by=created_by)
                storage.update_user(_SIBLING, preset_key="guest")
                return row

        else:

            def damaged_writer(preset_key, name, capabilities, *, description="", created_by):
                return original(
                    preset_key, name, capabilities, description=description, created_by=created_by
                )

            original_post = ctx["client"].post

            def damaged_post(url, *args, **kwargs):
                response = original_post(url, *args, **kwargs)
                if url == "/api/admin/presets" and response.status_code == 200:
                    payload = response.json()
                    payload["preset"]["name"] = "Corrupted HTTP-only name"
                    return httpx.Response(response.status_code, json=payload, request=response.request)
                return response

            monkeypatch.setattr(ctx["client"], "post", damaged_post)
        monkeypatch.setattr(storage, "upsert_custom_preset", damaged_writer)
    elif fault == "audit_missing":
        import friday.admin_api._users as users_api

        monkeypatch.setattr(users_api, "_audit", lambda *_args, **_kwargs: None)
    elif fault == "audit_unsanitized":
        from friday.audit_privacy import decode_audit_privacy_key, sanitize_audit_target

        def damaged_audit_writer(entry):
            row = entry.to_row()
            key_row = storage.execute(
                "SELECT value FROM schema_meta WHERE key='audit_privacy_hmac_key'"
            ).fetchone()
            privacy_key = decode_audit_privacy_key(key_row[0])
            target_type, target_id = sanitize_audit_target(
                entry.target_type,
                entry.target_id,
                key=privacy_key,
                user_exists=lambda candidate: (
                    storage.execute("SELECT 1 FROM users WHERE id=? LIMIT 1", (candidate,)).fetchone()
                    is not None
                ),
            )
            row["target_type"] = target_type
            row["target_id"] = target_id
            with storage.transaction() as conn:
                conn.execute(
                    """INSERT INTO audit_log(id, user_id, action, target_type, target_id,
                           before_json, after_json, ip_address, request_id, created_at)
                       VALUES(:id, :user_id, :action, :target_type, :target_id,
                           :before_json, :after_json, :ip_address, :request_id, :created_at)""",
                    row,
                )
            return entry

        monkeypatch.setattr(storage, "log_audit", damaged_audit_writer)
    elif fault == "refusal_hidden_write":
        original = ctx["client"].post
        injected = False

        def damaged_refusal(url, *args, **kwargs):
            nonlocal injected
            if url == "/api/admin/presets" and kwargs.get("json") == {} and not injected:
                injected = True
                original(
                    url,
                    headers=kwargs.get("headers"),
                    json={
                        "preset_key": "r10_hidden_refusal_write",
                        "name": "Hidden private write",
                        "capabilities": ["chat.use"],
                    },
                )
                return httpx.Response(
                    400,
                    json={"detail": "synthetic refusal after accepted write"},
                    request=httpx.Request("POST", "http://testserver/api/admin/presets"),
                )
            return original(url, *args, **kwargs)

        monkeypatch.setattr(ctx["client"], "post", damaged_refusal)
    else:  # pragma: no cover - the closed parameter list owns every branch
        raise AssertionError(fault)


@pytest.mark.parametrize(
    ("fault", "oracle", "failure_code"),
    (
        ("capability_drop", "capabilities", "capability_count"),
        ("capability_metadata", "capabilities", "capability_anchor"),
        ("preset_membership", "presets", "preset_builtin_membership"),
        ("update_stale_authority", "update_authority", "preset_update_subsequent_personal_authority"),
        ("create_unwritten", "create", "preset_create_persisted"),
        ("create_wrong_capabilities", "create", "preset_create_fields"),
        ("create_collateral_preset", "create", "preset_create_preserves_other_presets"),
        ("create_collateral_account", "create", "preset_create_preserves_accounts"),
        ("create_response", "create", "preset_create_http_storage"),
        ("audit_missing", "create", "preset_create_audit_count"),
        ("audit_unsanitized", "create", "preset_create_audit_after"),
        ("refusal_hidden_write", "refusals", "preset_post_refusal_no_effect"),
        ("create_override", "create", "preset_create_preserves_policy"),
        ("refusal_override", "refusals", "preset_post_refusal_no_effect"),
        ("create_orphan_capability", "create", "preset_create_capability_rows"),
        ("create_account_creation", "create", "preset_create_preserves_accounts"),
    ),
    ids=(
        "capability_drop",
        "capability_metadata",
        "preset_membership",
        "update_stale_authority",
        "create_unwritten",
        "create_wrong_capabilities",
        "create_collateral_preset",
        "create_collateral_account",
        "create_response",
        "audit_missing",
        "audit_unsanitized",
        "refusal_hidden_write",
        "create_override",
        "refusal_override",
        "create_orphan_capability",
        "create_account_creation",
    ),
)
def test_preset_oracles_detect_real_http_output_storage_and_writer_faults(
    preset_http, monkeypatch, fault, oracle, failure_code
):
    _install_fault(preset_http, monkeypatch, fault)
    with pytest.raises(AssertionError, match=failure_code):
        if oracle == "capabilities":
            _assert_capability_catalog(preset_http)
        elif oracle == "presets":
            _assert_preset_catalog(preset_http)
        elif oracle == "create":
            _assert_owner_create(preset_http, monkeypatch)
        elif oracle == "update_authority":
            _assert_update_authority(preset_http)
        else:
            _assert_invalid_post_refusals(preset_http)
