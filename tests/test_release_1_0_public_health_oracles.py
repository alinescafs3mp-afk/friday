"""GET /health and /api/health: public aliases, closed projection, privacy, no effects."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace

import httpx
import pytest
from fastapi.testclient import TestClient

import friday
from friday.knowledge_graph import KnowledgeGraph
from friday.orchestration.contracts import RouterMode
from friday.server import create_app
from friday.storage.models import EntityType
from tests.test_api_tokens import _issue
from tests.test_organs_profile_chronicle import _seed_knowledge
from tests.test_release_1_0_conversation_oracles import _body, _equal
from tests.test_release_1_0_knowledge_read_oracles import _audits, _read_state

_PATHS = ("/health", "/api/health")
_PATH_KEYS = ("home", "vault_path", "obsidian_root")
_OWNER = "local:r10-ops-health-owner"
_ADMIN = "local:r10-ops-health-admin"
_USER = "local:r10-ops-health-user"
_A = "local:r10-ops-health-a"
_B = "local:r10-ops-health-b"
_LLM_KEY = "sk-CANARY-HEALTH-LLM-" + "L" * 20
_EMB_KEY = "sk-CANARY-HEALTH-EMB-" + "E" * 20
_SEC_KEY = "sk-CANARY-HEALTH-SEC-" + "S" * 20
_BRIDGE_CANARY = "CANARY-HEALTH-BRIDGE-" + "B" * 16
_LLM_MODEL = "ops-health-closed-model"
_OWN_TITLE = "OWN_PUBLIC_HEALTH_KO"
_FOREIGN_TITLE = "FOREIGN_PRIVATE_HEALTH_KO"
_VAULT_MODE = "disabled"
_INVALID = {"Authorization": "Bearer jrc_INVALID_HEALTH_TOKEN"}
_CREDENTIAL_ACTORS = ("owner", "invalid")
_SEEDED_AUDIT_ID = "aud_seeded_public_health_history"
_SEEDED_AUDIT_ACTION = "public-health-prior-history"
_CLOSED_SECONDARY = {
    "schema": "friday.optional-secondary-health.v1",
    "role": "optional_advisory",
    "enabled": False,
    "configured": False,
    "mode": "disabled",
    "state": "disabled",
    "available": False,
}


def _leak_markers(ctx: dict) -> tuple[str, ...]:
    return ctx["secrets"] + _path_markers(ctx)


def _path_markers(ctx: dict) -> tuple[str, ...]:
    return tuple(ctx[key] for key in _PATH_KEYS)


def _assert_no_markers(text: str, markers: tuple[str, ...], code: str) -> None:
    for marker in markers:
        assert marker not in text, code


def _assert_state_snapshot(state: dict) -> None:
    assert state["users"] and state["knowledge_objects"], "health_state_not_empty"


def _path_of(url: object) -> str:
    raw = str(url).split("?")[0]
    if "://" in raw:
        raw = "/" + raw.split("/", 3)[-1]
    return raw if raw.startswith("/") else "/" + raw


def _is_target_health(method: str, url: object, path: str) -> bool:
    return method == "GET" and _path_of(url) == path


def _other_path(path: str) -> str:
    return _PATHS[1] if path == _PATHS[0] else _PATHS[0]


def _headers_for(ctx: dict, actor: str) -> dict[str, str]:
    if actor == "anon":
        return {}
    if actor == "invalid":
        return _INVALID
    return ctx["headers"][actor]


def _submitted_credential(headers: dict[str, str]) -> str:
    value = headers.get("Authorization", "")
    assert value.startswith("Bearer "), "health_credential_fixture"
    secret = value.removeprefix("Bearer ")
    assert secret, "health_credential_fixture"
    return secret


def _other_actor(actor: str) -> str:
    return _CREDENTIAL_ACTORS[1] if actor == _CREDENTIAL_ACTORS[0] else _CREDENTIAL_ACTORS[0]


def _request_authorization(response: httpx.Response) -> str:
    request = response.request
    if request is None:
        return ""
    return request.headers.get("authorization", "")


def _expected(ctx: dict, status: str) -> dict[str, object]:
    return {
        "status": status,
        "version": friday.__version__,
        "llm_enabled": False,
        "model": _LLM_MODEL,
        "profile": ctx["profile"],
        "secondary": dict(_CLOSED_SECONDARY),
        "vault_mode": _VAULT_MODE,
        "vault_body_free": True,
        "vault_body_projection": False,
        "obsidian_mode": ctx["obsidian_mode"],
        "obsidian_root_sha256": ctx["obsidian_root_sha256"],
        "orchestration_schema": "friday.v12-orchestration-health.v1",
        "configured_mode": ctx["router_mode"],
        "installed_mode": RouterMode.LEGACY.value,
        "model_gate_status": "not_installed",
        "model_gate_reason": "mode_does_not_require_live_attestation",
        "embeddings_index_absent": True,
        "rerank_absent": True,
        "live_llm_not_ready": True,
    }


def _observed(body: dict[str, object]) -> dict[str, object]:
    assert "status" in body, "health_status"
    assert "version" in body, "health_version"
    assert "llm_enabled" in body, "health_llm"
    assert "secondary" in body, "health_secondary"
    assert "memory_vault" in body, "health_vault"
    assert "orchestration" in body, "health_orchestration"
    secondary = body["secondary"]
    vault = body["memory_vault"]
    orch = body["orchestration"]
    assert isinstance(secondary, dict) and "state" in secondary, "health_secondary"
    assert isinstance(vault, dict) and "body_projection_enabled" in vault, "health_vault"
    assert isinstance(orch, dict) and "model_gate" in orch, "health_orchestration"
    gate = orch["model_gate"]
    assert isinstance(gate, dict) and "status" in gate, "health_orchestration"
    obsidian = body.get("obsidian")
    assert isinstance(obsidian, dict), "health_obsidian"
    return {
        "status": body["status"],
        "version": body["version"],
        "llm_enabled": body["llm_enabled"],
        "model": body["model"],
        "profile": body["profile"],
        "secondary": {
            "schema": secondary.get("schema"),
            "role": secondary.get("role"),
            "enabled": secondary.get("enabled"),
            "configured": secondary.get("configured"),
            "mode": secondary.get("mode"),
            "state": secondary["state"],
            "available": secondary.get("available"),
        },
        "vault_mode": vault.get("mode"),
        "vault_body_free": vault.get("body_free_mode"),
        "vault_body_projection": vault["body_projection_enabled"],
        "obsidian_mode": obsidian.get("mode"),
        "obsidian_root_sha256": obsidian.get("root_sha256"),
        "orchestration_schema": orch.get("schema"),
        "configured_mode": orch.get("configured_mode"),
        "installed_mode": orch.get("installed_mode"),
        "model_gate_status": gate["status"],
        "model_gate_reason": gate.get("reason_code"),
        "embeddings_index_absent": "embeddings_index" not in body and "embeddings" not in body,
        "rerank_absent": "rerank" not in body,
        "live_llm_not_ready": body["llm_enabled"] is False and gate["status"] != "ready",
    }


def _assert_projection(body: dict[str, object], ctx: dict, status: str) -> None:
    _equal(body.get("status"), status, "health_status")
    _equal(body.get("version"), friday.__version__, "health_version")
    _equal(body.get("llm_enabled"), False, "health_llm")
    secondary = body.get("secondary")
    assert isinstance(secondary, dict) and "state" in secondary, "health_secondary"
    _equal(secondary["state"], "disabled", "health_secondary")
    vault = body.get("memory_vault")
    assert isinstance(vault, dict) and "body_projection_enabled" in vault, "health_vault"
    _equal(vault["body_projection_enabled"], False, "health_vault")
    assert "path" not in vault, "health_vault_path_absent"
    _equal(_observed(body), _expected(ctx, status), "health_projection")


def _assert_privacy(ctx: dict, body: dict[str, object], raw_text: str) -> None:
    packed = json.dumps(body, ensure_ascii=False)
    _assert_no_markers(raw_text, _path_markers(ctx), "health_vault_path_absent")
    _assert_no_markers(packed, _path_markers(ctx), "health_vault_path_absent")
    _assert_no_markers(raw_text, ctx["secrets"], "health_secrets_absent")
    _assert_no_markers(packed, ctx["secrets"], "health_secrets_absent")
    _assert_no_markers(raw_text, (_OWN_TITLE,), "health_own_body")
    _assert_no_markers(packed, (_OWN_TITLE,), "health_own_body")
    _assert_no_markers(raw_text, (_FOREIGN_TITLE,), "health_foreign_body")
    _assert_no_markers(packed, (_FOREIGN_TITLE,), "health_foreign_body")


def _assert_prior_history(audit_before: list[dict]) -> None:
    assert audit_before, "health_audit_history"
    assert any(row.get("id") == _SEEDED_AUDIT_ID for row in audit_before), "health_audit_history"
    assert any(row.get("action") == _SEEDED_AUDIT_ACTION for row in audit_before), "health_audit_history"


def _assert_one(ctx: dict, path: str, actor: str, *, expected_status: str) -> None:
    before, audit_before = _read_state(ctx), _audits(ctx)
    _assert_state_snapshot(before)
    _assert_prior_history(audit_before)
    response = ctx["client"].get(path, headers=_headers_for(ctx, actor))
    body = _body(response)
    _assert_projection(body, ctx, expected_status)
    _assert_privacy(ctx, body, response.text)
    _equal(_read_state(ctx), before, "health_read_preserves_state")
    rows = _audits(ctx)
    _equal(rows[: len(audit_before)], audit_before, "health_audit_history")
    _equal(rows, audit_before, "health_audit_unchanged")
    packed_audit = json.dumps(rows, ensure_ascii=False)
    _assert_no_markers(packed_audit, _leak_markers(ctx), "health_audit_secrets_absent")
    _assert_no_markers(packed_audit, (_OWN_TITLE, _FOREIGN_TITLE), "health_audit_secrets_absent")


def _run_ok(ctx: dict, path: str) -> None:
    for actor in ("anon", "owner", "invalid"):
        _assert_one(ctx, path, actor, expected_status="ok")


def _run_starting(ctx: dict, path: str) -> None:
    app = ctx["app"]
    original = app.state.storage
    assert original is ctx["storage"]
    try:
        delattr(app.state, "storage")
        for actor in ("anon", "owner", "invalid"):
            _assert_one(ctx, path, actor, expected_status="starting")
    finally:
        app.state.storage = original


def _seed_prior_audit(storage, markers: tuple[str, ...]) -> None:
    with storage.transaction() as connection:
        connection.execute(
            "INSERT INTO audit_log(id, user_id, action, target_type, target_id, "
            "before_json, after_json, ip_address, request_id, created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                _SEEDED_AUDIT_ID,
                _A,
                _SEEDED_AUDIT_ACTION,
                "health",
                "prior-history",
                None,
                None,
                "",
                "",
                "2020-01-01T00:00:00+00:00",
            ),
        )
        found = connection.execute(
            "SELECT id, action, user_id, target_type, target_id FROM audit_log WHERE id=?",
            (_SEEDED_AUDIT_ID,),
        ).fetchone()
        assert found is not None, "health_audit_history"
        assert found["id"] == _SEEDED_AUDIT_ID, "health_audit_history"
        assert found["action"] == _SEEDED_AUDIT_ACTION, "health_audit_history"
        packed = json.dumps(dict(found), ensure_ascii=False)
        _assert_no_markers(packed, markers, "health_audit_secrets_absent")


@pytest.fixture
def health_http(settings):
    assert not settings.llm_enabled and not settings.workers_enabled
    vault = settings.home.parent / "CONFIGURED_HEALTH_VAULT_CANARY"
    vault.mkdir()
    obsidian = settings.home.parent / "CONFIGURED_HEALTH_OBSIDIAN_CANARY"
    obsidian.mkdir()
    assert all(
        str(left) not in str(right)
        for left in (settings.home, vault, obsidian)
        for right in (settings.home, vault, obsidian)
        if left != right
    ), "health_path_fixture_independence"
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
        telegram_bridge_secret=_BRIDGE_CANARY,
        llm_model=_LLM_MODEL,
        embeddings_base_url="http://127.0.0.1:9/v1",
        llm_base_url="http://127.0.0.1:9/v1",
        memory_vault_mode=_VAULT_MODE,
        memory_vault_dir=vault,
        obsidian_root=obsidian,
    )
    app = create_app(tuned)
    with TestClient(app) as client:
        storage = app.state.storage
        obsidian_root = str(tuned.obsidian_effective_root)
        ctx = {
            "app": app,
            "client": client,
            "storage": storage,
            "home": str(tuned.home),
            "vault_path": str(vault),
            "obsidian_root": obsidian_root,
            "profile": tuned.profile.name,
            "router_mode": tuned.router_mode,
            "obsidian_mode": "enabled" if tuned.obsidian_enabled else "disabled",
            "obsidian_root_sha256": hashlib.sha256(
                obsidian_root.encode("utf-8", errors="strict")
            ).hexdigest(),
            "secrets": (
                _LLM_KEY,
                _EMB_KEY,
                _SEC_KEY,
                _BRIDGE_CANARY,
                settings.api_token,
            ),
            "headers": {},
        }
        issued: list[str] = []
        for key, person, preset in (
            ("owner", _OWNER, "owner"),
            ("admin", _ADMIN, "admin"),
            ("user", _USER, "user"),
            ("a", _A, "user"),
            ("b", _B, "user"),
        ):
            secret = "jrc_synthetic_health_" + key
            _issue(storage, person, preset, secret)
            ctx["headers"][key] = {"Authorization": "Bearer " + secret}
            issued.append(_submitted_credential(ctx["headers"][key]))
        invalid_submitted = _submitted_credential(_INVALID)
        assert issued and all(issued) and invalid_submitted
        assert len(issued) == 5
        ctx["secrets"] = ctx["secrets"] + tuple(issued) + (invalid_submitted,)
        ctx["own"] = _seed_knowledge(storage, _A, _OWN_TITLE, ["own"])
        ctx["foreign"] = _seed_knowledge(storage, _B, _FOREIGN_TITLE, ["foreign"])
        graph = KnowledgeGraph(storage)
        person = graph.create_entity(_A, "Health Person", EntityType.PERSON)
        box = graph.create_container(_A, "Health Box", kind="collection")
        ctx["person"] = person["id"]
        ctx["box"] = box["id"]
        _seed_prior_audit(storage, _leak_markers(ctx) + (_OWN_TITLE, _FOREIGN_TITLE))
        yield ctx


@pytest.mark.parametrize("path", _PATHS, ids=["health", "api_health"])
def test_public_health_ok_with_storage_anon_owner_invalid_token(health_http, path):
    _run_ok(health_http, path)


@pytest.mark.parametrize("path", _PATHS, ids=["health", "api_health"])
def test_public_health_starting_without_storage_anon_owner_invalid_token(health_http, path):
    _run_starting(health_http, path)


_FAULTS = [
    ("wrong_status", "health_status"),
    ("wrong_version", "health_version"),
    ("wrong_llm", "health_llm"),
    ("wrong_secondary", "health_secondary"),
    ("wrong_vault", "health_vault"),
    ("secret_in_200", "health_secrets_absent"),
    ("own_body", "health_own_body"),
    ("foreign_body", "health_foreign_body"),
    ("own_ko", "health_read_preserves_state"),
    ("permission_write", "health_read_preserves_state"),
    ("audit_prefix", "health_audit_history"),
]

assert len(_PATHS) == 2
assert len(_FAULTS) == 11
assert len(_CREDENTIAL_ACTORS) == 2
assert len(_PATHS) * (len(_FAULTS) + len(_CREDENTIAL_ACTORS) + len(_PATH_KEYS)) + 4 == 36


def _fault_row(connection, table, row_id, field, value) -> bool:
    found = connection.execute(f"SELECT {field} FROM {table} WHERE id=?", (row_id,)).fetchone()
    return found is not None and found[field] == value


@pytest.mark.parametrize("fault,code", _FAULTS, ids=[row[0] for row in _FAULTS])
@pytest.mark.parametrize("path", _PATHS, ids=["health", "api_health"])
def test_public_health_oracles_detect_corrupted_http_and_writes(health_http, monkeypatch, fault, code, path):
    ctx, storage = health_http, health_http["storage"]
    original = TestClient.request
    hits: list[str] = []

    def note() -> None:
        hits.append(fault)

    def altered(self, method, url, **kwargs):
        method = method.upper()
        response = original(self, method, url, **kwargs)
        if not _is_target_health(method, url, path):
            return response
        injected = False
        replacement = response
        if fault == "wrong_status" and response.status_code == 200:
            body = response.json()
            body["status"] = "starting" if body.get("status") == "ok" else "ok"
            injected = True
            replacement = httpx.Response(200, json=body, request=response.request)
        if fault == "wrong_version" and response.status_code == 200:
            body = response.json()
            body["version"] = "0.0.0-not-friday"
            injected = True
            replacement = httpx.Response(200, json=body, request=response.request)
        if fault == "wrong_llm" and response.status_code == 200:
            body = response.json()
            body["llm_enabled"] = True
            injected = True
            replacement = httpx.Response(200, json=body, request=response.request)
        if fault == "wrong_secondary" and response.status_code == 200:
            body = response.json()
            body["secondary"].pop("state", None)
            injected = True
            replacement = httpx.Response(200, json=body, request=response.request)
        if fault == "wrong_vault" and response.status_code == 200:
            body = response.json()
            body["memory_vault"]["body_projection_enabled"] = True
            injected = True
            replacement = httpx.Response(200, json=body, request=response.request)
        if fault == "secret_in_200" and response.status_code == 200:
            body = response.json()
            body["llm_api_key"] = _LLM_KEY
            injected = True
            replacement = httpx.Response(200, json=body, request=response.request)
        if fault == "own_body" and response.status_code == 200:
            body = response.json()
            body["note"] = _OWN_TITLE
            injected = True
            replacement = httpx.Response(200, json=body, request=response.request)
        if fault == "foreign_body" and response.status_code == 200:
            body = response.json()
            body["note"] = _FOREIGN_TITLE
            injected = True
            replacement = httpx.Response(200, json=body, request=response.request)
        if fault == "own_ko" and response.status_code == 200:
            with storage.transaction() as connection:
                connection.execute(
                    "UPDATE knowledge_objects SET title='corrupted-own' WHERE id=?",
                    (ctx["own"],),
                )
                if _fault_row(connection, "knowledge_objects", ctx["own"], "title", "corrupted-own"):
                    injected = True
        if fault == "permission_write" and response.status_code == 200 and not ctx.get("_override_injected"):
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
                    injected = True
        if fault == "audit_prefix" and response.status_code == 200 and not ctx.get("_prefix_injected"):
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
                        "aud_corrupted_health_prefix",
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
                    ("aud_corrupted_health_prefix",),
                ).fetchone()
                first = connection.execute("SELECT id FROM audit_log ORDER BY rowid LIMIT 1").fetchone()
                seed = connection.execute(
                    "SELECT id, action FROM audit_log WHERE id=?",
                    (_SEEDED_AUDIT_ID,),
                ).fetchone()
                if (
                    found is not None
                    and found["action"] == "corrupted-prefix"
                    and first is not None
                    and first["id"] == "aud_corrupted_health_prefix"
                    and seed is not None
                    and seed["action"] == _SEEDED_AUDIT_ACTION
                    and seed["id"] == _SEEDED_AUDIT_ID
                ):
                    injected = True
        if injected:
            note()
            ctx.setdefault("_hit_paths", set()).add(path)
            return replacement
        return response

    monkeypatch.setattr(TestClient, "request", altered)
    with pytest.raises(AssertionError, match=code):
        _run_ok(ctx, path)
    assert hits, f"health_fault_not_injected:{fault}"
    _equal(ctx.get("_hit_paths"), {path}, f"health_fault_missed_alias:{fault}")
    assert _other_path(path) not in ctx.get("_hit_paths", set()), f"health_fault_other_alias_injected:{fault}"


@pytest.mark.parametrize("actor", _CREDENTIAL_ACTORS, ids=list(_CREDENTIAL_ACTORS))
@pytest.mark.parametrize("path", _PATHS, ids=["health", "api_health"])
def test_public_health_oracles_detect_submitted_credential_in_200(health_http, monkeypatch, path, actor):
    ctx = health_http
    submitted = _submitted_credential(_headers_for(ctx, actor))
    original = TestClient.request
    hits: list[str] = []

    def note() -> None:
        hits.append(actor)

    def altered(self, method, url, **kwargs):
        method = method.upper()
        response = original(self, method, url, **kwargs)
        if not _is_target_health(method, url, path):
            return response
        if response.status_code != 200:
            return response
        sent = _request_authorization(response)
        if sent != "Bearer " + submitted:
            return response
        body = response.json()
        body["authorization"] = submitted
        packed = json.dumps(body, ensure_ascii=False)
        assert submitted in packed, "health_fault_not_injected:submitted_credential"
        ctx.setdefault("_hit_paths", set()).add(path)
        ctx.setdefault("_hit_actors", set()).add(actor)
        note()
        return httpx.Response(200, json=body, request=response.request)

    monkeypatch.setattr(TestClient, "request", altered)
    with pytest.raises(AssertionError, match="health_secrets_absent"):
        _assert_one(ctx, path, actor, expected_status="ok")
    assert hits, "health_fault_not_injected:submitted_credential"
    _equal(ctx.get("_hit_paths"), {path}, "health_fault_missed_alias:submitted_credential")
    _equal(ctx.get("_hit_actors"), {actor}, "health_fault_missed_actor:submitted_credential")
    assert _other_path(path) not in ctx.get("_hit_paths", set()), (
        "health_fault_other_alias_injected:submitted_credential"
    )
    assert _other_actor(actor) not in ctx.get("_hit_actors", set()), (
        "health_fault_other_actor_injected:submitted_credential"
    )


@pytest.mark.parametrize("marker_key", _PATH_KEYS)
@pytest.mark.parametrize("path", _PATHS, ids=["health", "api_health"])
def test_public_health_oracles_detect_configured_path_in_200(health_http, monkeypatch, path, marker_key):
    ctx, hits = health_http, []
    marker = ctx[marker_key]
    assert marker and marker in _path_markers(ctx), "health_path_fixture"
    original = TestClient.request

    def altered(self, method, url, **kwargs):
        response = original(self, method, url, **kwargs)
        if _is_target_health(method.upper(), url, path) and response.status_code == 200:
            body = response.json()
            body["configured_path"] = marker
            replacement = httpx.Response(200, json=body, request=response.request)
            assert marker in replacement.text, "health_path_fault_injected"
            hits.append(path)
            return replacement
        return response

    monkeypatch.setattr(TestClient, "request", altered)
    with pytest.raises(AssertionError, match="health_vault_path_absent"):
        _assert_one(ctx, path, "anon", expected_status="ok")
    _equal(hits, [path], "health_path_fault_exact_alias")
