"""GET /api/admin/diagnostics: real collector baseline, wrapper plumbing, authority."""

from __future__ import annotations

import json
from dataclasses import replace

import httpx
import pytest
from fastapi.testclient import TestClient

import friday.admin_api._overview as overview_mod
from friday.knowledge_graph import KnowledgeGraph
from friday.server import create_app
from friday.storage.models import EntityType
from tests.test_api_tokens import _issue
from tests.test_organs_profile_chronicle import _seed_knowledge
from tests.test_release_1_0_conversation_oracles import _body, _equal
from tests.test_release_1_0_knowledge_read_oracles import _audits, _read_state

_PATH = "/api/admin/diagnostics"
_OWNER = "local:r10-ops-diag-owner"
_ADMIN = "local:r10-ops-diag-admin"
_USER = "local:r10-ops-diag-user"
_A = "local:r10-ops-diag-a"
_B = "local:r10-ops-diag-b"
_LLM_KEY = "sk-CANARY-DIAG-LLM-" + "L" * 20
_EMB_KEY = "sk-CANARY-DIAG-EMB-" + "E" * 20
_SEC_KEY = "sk-CANARY-DIAG-SEC-" + "S" * 20
_OWN_TITLE = "OWN_DIAGNOSTICS_KO"
_FOREIGN_TITLE = "FOREIGN_PRIVATE_DIAGNOSTICS_KO"
_VAULT_MODE = "disabled"
_SECONDARY_SCHEMA = "friday.optional-secondary-health.v1"
_CLOSED_COLLECTOR = {
    "ok": True,
    "actions": [],
    "features": {"llm_enabled": False},
    "configuration_issues": [],
    "plumbing": "closed-synthetic-collector",
}


def _leak_markers(ctx: dict) -> tuple[str, ...]:
    return ctx["secrets"] + (ctx["vault_path"],) + ctx["body_canaries"]


def _assert_no_markers(text: str, markers: tuple[str, ...], code: str) -> None:
    for marker in markers:
        assert marker not in text, code


def _assert_state_snapshot(state: dict) -> None:
    assert state["users"] and state["knowledge_objects"], "diagnostics_state_not_empty"


def _assert_authorized_vault(body: dict[str, object], vault_path: str) -> None:
    assert "paths" in body and "vault" in body["paths"], "diagnostics_vault_path"
    vault = body["paths"]["vault"]
    assert "path" in vault, "diagnostics_vault_path"
    _equal(vault["path"], vault_path, "diagnostics_vault_path")
    _equal(vault["exists"], True, "diagnostics_vault_exists")
    _equal(vault["is_directory"], True, "diagnostics_vault_is_directory")


def _assert_success_privacy(ctx: dict, body: dict[str, object], raw_text: str) -> None:
    _assert_authorized_vault(body, ctx["vault_path"])
    clone = json.loads(json.dumps(body))
    del clone["paths"]["vault"]["path"]
    _assert_no_markers(
        json.dumps(clone, ensure_ascii=False), _leak_markers(ctx), "diagnostics_secrets_absent"
    )
    _assert_no_markers(raw_text, ctx["secrets"] + ctx["body_canaries"], "diagnostics_secrets_absent")


def _assert_refusal_privacy(ctx: dict, text: str) -> None:
    _assert_no_markers(text, _leak_markers(ctx), "diagnostics_refusal_secrets_absent")


def _assert_plumbing_privacy(ctx: dict, body: dict[str, object], raw_text: str) -> None:
    # Spy collector has no real paths; this is not authorized-vault proof.
    paths = body.get("paths")
    assert not isinstance(paths, dict) or "vault" not in paths, "diagnostics_plumbing_no_real_vault"
    _assert_no_markers(json.dumps(body, ensure_ascii=False), _leak_markers(ctx), "diagnostics_secrets_absent")
    _assert_no_markers(raw_text, _leak_markers(ctx), "diagnostics_secrets_absent")


def _assert_clean_delta(rows: list, audit_before: list, *, success: bool) -> None:
    _equal(rows[: len(audit_before)], audit_before, "diagnostics_audit_history")
    delta = rows[len(audit_before) :]
    assert not any(str(row["action"]).startswith("admin.") for row in delta), (
        "diagnostics_success_no_admin_audit" if success else "diagnostics_refusal_no_success_audit"
    )
    if success:
        _equal(delta, [], "diagnostics_success_no_audit")
    return delta


def _expected_real(home: str, vault_path: str) -> dict[str, object]:
    # Closed values from this module's fixture constants, not collect_diagnostics().
    return {
        "ok": True,
        "home_path": home,
        "home_exists": True,
        "home_is_directory": True,
        "vault_path": vault_path,
        "vault_exists": True,
        "vault_is_directory": True,
        "llm_enabled": False,
        "embeddings_enabled": False,
        "workers_enabled": False,
        "code_execution_enabled": False,
        "web_private_networks_allowed": False,
        "llm_endpoint_absent": True,
        "embeddings_endpoint_absent": True,
        "rerank_endpoint_absent": True,
        "probe_actions_absent": True,
        "embeddings_index": {
            "available": True,
            "indexed_objects": 0,
            "chunked_objects": 0,
            "chunk_rows": 0,
        },
        "secondary_schema": _SECONDARY_SCHEMA,
        "secondary_role": "optional_advisory",
        "secondary_enabled": False,
        "secondary_configured": False,
        "secondary_mode": "disabled",
        "secondary_state": "disabled",
        "secondary_available": False,
    }


def _observed_real(body: dict[str, object]) -> dict[str, object]:
    assert "ok" in body, "diagnostics_projection_ok"
    assert "actions" in body, "diagnostics_projection_actions"
    assert "features" in body, "diagnostics_projection_features_llm"
    features = body["features"]
    assert "llm_enabled" in features, "diagnostics_projection_features_llm"
    assert "secondary" in body, "diagnostics_secondary_state"
    secondary = body["secondary"]
    assert "state" in secondary, "diagnostics_secondary_state"
    vault = body["paths"]["vault"]
    codes = {str(item.get("code")) for item in body["actions"]}
    return {
        "ok": body["ok"],
        "home_path": body["paths"]["home"]["path"],
        "home_exists": body["paths"]["home"]["exists"],
        "home_is_directory": body["paths"]["home"]["is_directory"],
        "vault_path": vault["path"],
        "vault_exists": vault["exists"],
        "vault_is_directory": vault["is_directory"],
        "llm_enabled": features["llm_enabled"],
        "embeddings_enabled": features["embeddings_enabled"],
        "workers_enabled": features["workers_enabled"],
        "code_execution_enabled": features["code_execution_enabled"],
        "web_private_networks_allowed": features["web_private_networks_allowed"],
        "llm_endpoint_absent": "llm_endpoint" not in body,
        "embeddings_endpoint_absent": "embeddings_endpoint" not in body,
        "rerank_endpoint_absent": "rerank_endpoint" not in body,
        "probe_actions_absent": "start_llm_runtime" not in codes and "start_embeddings_runtime" not in codes,
        "embeddings_index": body["embeddings_index"],
        "secondary_schema": secondary["schema"],
        "secondary_role": secondary["role"],
        "secondary_enabled": secondary["enabled"],
        "secondary_configured": secondary["configured"],
        "secondary_mode": secondary["mode"],
        "secondary_state": secondary["state"],
        "secondary_available": secondary["available"],
    }


def _assert_real_projection(body: dict[str, object], home: str, vault_path: str) -> None:
    assert "ok" in body, "diagnostics_projection_ok"
    _equal(body["ok"], True, "diagnostics_projection_ok")
    assert "actions" in body, "diagnostics_projection_actions"
    assert isinstance(body["actions"], list), "diagnostics_projection_actions"
    features = body["features"]
    assert "llm_enabled" in features, "diagnostics_projection_features_llm"
    _equal(features["llm_enabled"], False, "diagnostics_projection_features_llm")
    secondary = body["secondary"]
    assert "state" in secondary, "diagnostics_secondary_state"
    _equal(secondary["state"], "disabled", "diagnostics_secondary_state")
    _assert_authorized_vault(body, vault_path)
    _equal(_observed_real(body), _expected_real(home, vault_path), "diagnostics_projection")


def _get(ctx: dict, actor: str, params=None):
    return ctx["client"].get(_PATH, headers=ctx["headers"][actor], params=params)


def _assert_success(ctx: dict, actor: str) -> None:
    for params in (None, {"check_llm": False}, {"check_llm": True}):
        before, audit_before = _read_state(ctx), _audits(ctx)
        _assert_state_snapshot(before)
        response = _get(ctx, actor, params)
        body = _body(response)
        _assert_real_projection(body, ctx["home"], ctx["vault_path"])
        _assert_success_privacy(ctx, body, response.text)
        assert body.get("plumbing") != "closed-synthetic-collector", (
            "diagnostics_real_collector_not_plumbing_spy"
        )
        _equal(_read_state(ctx), before, "diagnostics_read_preserves_state")
        rows = _audits(ctx)
        _assert_clean_delta(rows, audit_before, success=True)
        _assert_no_markers(
            json.dumps(rows, ensure_ascii=False),
            _leak_markers(ctx),
            "diagnostics_audit_secrets_absent",
        )


def _assert_refusals(ctx: dict) -> None:
    ctx["storage"].set_permission_override(_ADMIN, "admin.diagnostics", "deny")
    before = _read_state(ctx)
    _assert_state_snapshot(before)
    cases = (
        ({}, None, 401, "diagnostics_401"),
        (ctx["headers"]["user"], None, 403, "diagnostics_403_ordinary"),
        (ctx["headers"]["admin"], None, 403, "diagnostics_403_revoked"),
        (ctx["headers"]["owner"], {"check_llm": "not-a-bool"}, 422, "diagnostics_422"),
    )
    for headers, params, status, code in cases:
        snap, audit_snap = _read_state(ctx), _audits(ctx)
        _assert_state_snapshot(snap)
        response = ctx["client"].get(_PATH, headers=headers, params=params)
        _equal(response.status_code, status, code)
        _assert_refusal_privacy(ctx, response.text)
        _equal(_read_state(ctx), snap, "diagnostics_refusal_no_effect")
        rows = _audits(ctx)
        delta = _assert_clean_delta(rows, audit_snap, success=False)
        if status == 401:
            _equal(
                [(row["user_id"], row["action"], row["target_type"], row["target_id"]) for row in delta],
                [("anonymous", "auth.failed", "auth", "invalid_credentials")],
                "diagnostics_401_auth_failed",
            )
        else:
            assert not any(row["action"] == "auth.failed" for row in delta), (
                "diagnostics_refusal_no_auth_failed"
            )
            _equal(delta, [], "diagnostics_refusal_no_audit")
        _assert_no_markers(
            json.dumps(rows, ensure_ascii=False),
            _leak_markers(ctx),
            "diagnostics_audit_secrets_absent",
        )
    _equal(_read_state(ctx), before, "diagnostics_refusal_no_effect")


def _assert_plumbing(ctx: dict, monkeypatch) -> None:
    seen: list[bool] = []

    def fake(settings, storage=None, *, check_llm_port: bool = False, check_secrets: bool = False):
        del settings, storage, check_secrets
        seen.append(bool(check_llm_port))
        return dict(_CLOSED_COLLECTOR)

    monkeypatch.setattr(overview_mod, "collect_diagnostics", fake)
    for params in (None, {"check_llm": False}, {"check_llm": True}):
        before, audit_before = _read_state(ctx), _audits(ctx)
        _assert_state_snapshot(before)
        response = _get(ctx, "owner", params)
        body = _body(response)
        assert body.get("plumbing") == "closed-synthetic-collector", (
            "diagnostics_plumbing_uses_closed_collector"
        )
        assert "secondary" in body and "state" in body["secondary"], "diagnostics_secondary_state"
        _equal(body["secondary"]["state"], "disabled", "diagnostics_secondary_state")
        _assert_plumbing_privacy(ctx, body, response.text)
        _equal(_read_state(ctx), before, "diagnostics_read_preserves_state")
        rows = _audits(ctx)
        _assert_clean_delta(rows, audit_before, success=True)
    _equal(seen, [False, False, True], "diagnostics_query_forward")


@pytest.fixture
def diagnostics_http(settings):
    assert not settings.llm_enabled and not settings.workers_enabled
    vault = settings.home.parent / "CONFIGURED_DIAG_VAULT_CANARY"
    vault.mkdir()
    tuned = replace(
        settings,
        shared_archive=True,
        telegram_owner_chat_ids=[],
        llm_enabled=False,
        workers_enabled=False,
        embeddings_enabled=False,
        secondary_llm_enabled=False,
        secondary_llm_mode="disabled",
        code_execution_enabled=False,
        web_allow_private_networks=False,
        llm_api_key=_LLM_KEY,
        embeddings_api_key=_EMB_KEY,
        secondary_llm_api_key=_SEC_KEY,
        embeddings_base_url="http://127.0.0.1:9/v1",
        llm_base_url="http://127.0.0.1:9/v1",
        memory_vault_mode=_VAULT_MODE,
        memory_vault_dir=vault,
    )
    app = create_app(tuned)
    with TestClient(app) as client:
        storage = app.state.storage
        ctx = {
            "client": client,
            "storage": storage,
            "home": str(tuned.home),
            "vault_path": str(vault),
            "secrets": (
                _LLM_KEY,
                _EMB_KEY,
                _SEC_KEY,
                settings.api_token,
                settings.telegram_bridge_secret,
            ),
            "body_canaries": (_OWN_TITLE, _FOREIGN_TITLE),
            "headers": {},
        }
        for key, person, preset in (
            ("owner", _OWNER, "owner"),
            ("admin", _ADMIN, "admin"),
            ("user", _USER, "user"),
            ("a", _A, "user"),
            ("b", _B, "user"),
        ):
            secret = "jrc_synthetic_diag_" + key
            _issue(storage, person, preset, secret)
            ctx["headers"][key] = {"Authorization": "Bearer " + secret}
        ctx["own"] = _seed_knowledge(storage, _A, _OWN_TITLE, ["own"])
        ctx["foreign"] = _seed_knowledge(storage, _B, _FOREIGN_TITLE, ["foreign"])
        graph = KnowledgeGraph(storage)
        person = graph.create_entity(_A, "Diagnostics Person", EntityType.PERSON)
        box = graph.create_container(_A, "Diagnostics Box", kind="collection")
        ctx["person"] = person["id"]
        ctx["box"] = box["id"]
        yield ctx


def test_owner_and_delegated_diagnostics_http_real_collector_privacy_and_no_effects(
    diagnostics_http,
):
    _assert_success(diagnostics_http, "owner")
    _assert_success(diagnostics_http, "admin")


def test_diagnostics_http_wrapper_forwards_check_llm_query_without_generation(diagnostics_http, monkeypatch):
    _assert_plumbing(diagnostics_http, monkeypatch)


def test_diagnostics_http_anonymous_ordinary_revoked_and_malformed_refusals(diagnostics_http):
    _assert_refusals(diagnostics_http)


_FAULTS = [
    ("missing_ok", "diagnostics_projection_ok", "success"),
    ("missing_actions", "diagnostics_projection_actions", "success"),
    ("wrong_features", "diagnostics_projection_features_llm", "success"),
    ("missing_secondary_state", "diagnostics_secondary_state", "success"),
    ("wrong_query_forwarding", "diagnostics_query_forward", "plumbing"),
    ("secret_in_200", "diagnostics_secrets_absent", "success"),
    ("secret_in_refusal", "diagnostics_refusal_secrets_absent", "refusal"),
    ("own_ko", "diagnostics_read_preserves_state", "success"),
    ("refusal_override", "diagnostics_refusal_no_effect", "refusal"),
    ("secret_in_403", "diagnostics_refusal_secrets_absent", "refusal"),
    ("secret_in_422", "diagnostics_refusal_secrets_absent", "refusal"),
    ("audit_prefix", "diagnostics_audit_history", "refusal"),
]


def _fault_row(connection, table, row_id, field, value) -> bool:
    found = connection.execute(f"SELECT {field} FROM {table} WHERE id=?", (row_id,)).fetchone()
    return found is not None and found[field] == value


def _is_diagnostics(method: str, url: object) -> bool:
    return method == "GET" and str(url).split("?")[0].rstrip("/").endswith("/diagnostics")


def _inject_detail(response, marker: str):
    payload = response.json()
    if isinstance(payload, dict):
        payload["detail"] = str(payload.get("detail") or "") + marker
    return httpx.Response(response.status_code, json=payload, request=response.request)


@pytest.mark.parametrize("fault,code,scenario", _FAULTS, ids=[row[0] for row in _FAULTS])
def test_diagnostics_oracles_detect_corrupted_http_and_writes(
    diagnostics_http, monkeypatch, fault, code, scenario
):
    ctx, storage = diagnostics_http, diagnostics_http["storage"]
    original = TestClient.request
    hits: list[str] = []

    def note() -> None:
        hits.append(fault)

    def altered(self, method, url, **kwargs):
        method = method.upper()
        if fault == "wrong_query_forwarding" and _is_diagnostics(method, url):
            params = dict(kwargs.get("params") or {})
            current = params.get("check_llm")
            params["check_llm"] = current is not True
            kwargs["params"] = params
            note()
        response = original(self, method, url, **kwargs)
        if not _is_diagnostics(method, url):
            return response
        if fault == "missing_ok" and response.status_code == 200:
            body = response.json()
            body.pop("ok", None)
            note()
            return httpx.Response(200, json=body, request=response.request)
        if fault == "missing_actions" and response.status_code == 200:
            body = response.json()
            body.pop("actions", None)
            note()
            return httpx.Response(200, json=body, request=response.request)
        if fault == "wrong_features" and response.status_code == 200:
            body = response.json()
            body["features"]["llm_enabled"] = True
            note()
            return httpx.Response(200, json=body, request=response.request)
        if fault == "missing_secondary_state" and response.status_code == 200:
            body = response.json()
            body["secondary"].pop("state", None)
            note()
            return httpx.Response(200, json=body, request=response.request)
        if fault == "secret_in_200" and response.status_code == 200:
            body = response.json()
            body["llm_api_key"] = _LLM_KEY
            note()
            return httpx.Response(200, json=body, request=response.request)
        if fault == "secret_in_refusal" and response.status_code == 401:
            note()
            return _inject_detail(response, _LLM_KEY)
        if fault == "secret_in_403" and response.status_code == 403:
            note()
            return _inject_detail(response, ctx["vault_path"])
        if fault == "secret_in_422" and response.status_code == 422:
            note()
            return _inject_detail(response, ctx["vault_path"])
        if fault == "own_ko" and response.status_code == 200:
            with storage.transaction() as connection:
                connection.execute(
                    "UPDATE knowledge_objects SET title='corrupted-own' WHERE id=?",
                    (ctx["own"],),
                )
                if _fault_row(connection, "knowledge_objects", ctx["own"], "title", "corrupted-own"):
                    note()
        if fault == "refusal_override" and response.status_code == 403 and not ctx.get("_override_injected"):
            ctx["_override_injected"] = True
            with storage.transaction() as connection:
                connection.execute(
                    "INSERT INTO user_permission_overrides(user_id, security_id, effect, updated_at) "
                    "VALUES(?,?,?,?)",
                    (_USER, "knowledge.read", "deny", "2026-01-01T00:00:00+00:00"),
                )
                found = connection.execute(
                    "SELECT effect FROM user_permission_overrides WHERE user_id=? AND security_id=?",
                    (_USER, "knowledge.read"),
                ).fetchone()
                if found is not None and found["effect"] == "deny":
                    note()
        if fault == "audit_prefix" and response.status_code == 403 and not ctx.get("_prefix_injected"):
            ctx["_prefix_injected"] = True
            with storage.transaction() as connection:
                minimum = connection.execute("SELECT COALESCE(MIN(rowid), 1) AS m FROM audit_log").fetchone()[
                    "m"
                ]
                insert_rowid = int(minimum) - 1 if int(minimum) != 0 else -2
                connection.execute(
                    "INSERT INTO audit_log(rowid, id, user_id, action, target_type, target_id, "
                    "before_json, after_json, ip_address, request_id, created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        insert_rowid,
                        "aud_corrupted_prefix",
                        _USER,
                        "corrupted-prefix",
                        "audit",
                        "prefix",
                        None,
                        None,
                        "",
                        "",
                        "2026-01-01T00:00:00+00:00",
                    ),
                )
                found = connection.execute(
                    "SELECT id, action FROM audit_log WHERE id=?",
                    ("aud_corrupted_prefix",),
                ).fetchone()
                first = connection.execute("SELECT id FROM audit_log ORDER BY rowid LIMIT 1").fetchone()
                if (
                    found is not None
                    and found["action"] == "corrupted-prefix"
                    and first is not None
                    and first["id"] == "aud_corrupted_prefix"
                ):
                    note()
        return response

    monkeypatch.setattr(TestClient, "request", altered)
    with pytest.raises(AssertionError, match=code):
        if scenario == "refusal":
            _assert_refusals(ctx)
        elif scenario == "plumbing":
            _assert_plumbing(ctx, monkeypatch)
        else:
            _assert_success(ctx, "owner")
    assert hits, f"diagnostics_fault_not_injected:{fault}"
