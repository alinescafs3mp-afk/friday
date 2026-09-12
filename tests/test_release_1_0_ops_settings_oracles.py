"""GET /api/admin/settings: closed public projection, privacy, authority, no-effects."""

from __future__ import annotations

import json
from dataclasses import replace

import httpx
import pytest
from fastapi.testclient import TestClient

from friday.knowledge_graph import KnowledgeGraph
from friday.server import create_app
from friday.storage.models import EntityType
from tests.test_api_tokens import _issue
from tests.test_organs_profile_chronicle import _seed_knowledge
from tests.test_release_1_0_conversation_oracles import _body, _equal
from tests.test_release_1_0_knowledge_read_oracles import _audits, _read_state

_PATH = "/api/admin/settings"
_OWNER = "local:r10-ops-owner"
_ADMIN = "local:r10-ops-admin"
_USER = "local:r10-ops-user"
_A = "local:r10-ops-a"
_B = "local:r10-ops-b"
_LLM_KEY = "sk-CANARY-LLM-" + "L" * 24
_EMB_KEY = "sk-CANARY-EMB-" + "E" * 24
_LLM_MODEL = "ops-settings-closed-model"
_LLM_MAX_TOKENS = 128
_EMB_MODEL = "ops-settings-closed-emb"
_EMB_URL = "http://127.0.0.1:9/v1"
_VAULT_MODE = "disabled"
_OWN_TITLE = "OWN_SETTINGS_KO"
_FOREIGN_TITLE = "FOREIGN_PRIVATE_SETTINGS_KO"


def _expected(home: str) -> dict[str, object]:
    # Closed values from this module's fixture constants, not settings.public_dict().
    return {
        "home": home,
        "llm_enabled": False,
        "llm_auth": True,
        "llm_model": _LLM_MODEL,
        "llm_max_tokens": _LLM_MAX_TOKENS,
        "emb_enabled": False,
        "emb_effective": False,
        "emb_auth": True,
        "emb_model": _EMB_MODEL,
        "emb_base_url": _EMB_URL,
        "token_configured": True,
        "telegram_bridge_configured": True,
        "vault_mode": _VAULT_MODE,
        "vault_body_free": True,
        "secondary_enabled": False,
    }


def _observed(body: dict[str, object]) -> dict[str, object]:
    llm = body["llm"]
    embeddings = body["embeddings"]
    security = body["security"]
    vault = body["data"]["memory_vault"]
    assert "effective" in embeddings, "settings_projection_embeddings_effective"
    assert "auth" in llm, "settings_projection_llm_auth"
    return {
        "home": body["home"],
        "llm_enabled": llm["enabled"],
        "llm_auth": llm["auth"],
        "llm_model": llm["model"],
        "llm_max_tokens": llm["max_tokens"],
        "emb_enabled": embeddings["enabled"],
        "emb_effective": embeddings["effective"],
        "emb_auth": embeddings["auth"],
        "emb_model": embeddings["model"],
        "emb_base_url": embeddings["base_url"],
        "token_configured": security["token_configured"],
        "telegram_bridge_configured": security["telegram_bridge_configured"],
        "vault_mode": vault["mode"],
        "vault_body_free": vault["body_free_mode"],
        "secondary_enabled": body["secondary_llm"]["enabled"],
    }


def _assert_projection(body: dict[str, object], home: str) -> None:
    llm = body["llm"]
    embeddings = body["embeddings"]
    assert "auth" in llm, "settings_projection_llm_auth"
    _equal(llm["auth"], True, "settings_projection_llm_auth")
    assert "effective" in embeddings, "settings_projection_embeddings_effective"
    _equal(embeddings["effective"], False, "settings_projection_embeddings_effective")
    _equal(_observed(body), _expected(home), "settings_projection")


def _assert_no_canaries(ctx: dict, text: str, code: str) -> None:
    for marker in ctx["canaries"]:
        assert marker not in text, code


def _assert_success(ctx: dict, actor: str) -> None:
    before, audit_before = _read_state(ctx), _audits(ctx)
    response = ctx["client"].get(_PATH, headers=ctx["headers"][actor])
    body = _body(response)
    _assert_projection(body, ctx["home"])
    _assert_no_canaries(ctx, response.text, "settings_secrets_absent")
    _equal(_read_state(ctx), before, "settings_read_preserves_state")
    rows = _audits(ctx)
    _equal(rows[: len(audit_before)], audit_before, "settings_audit_history")
    delta = rows[len(audit_before) :]
    assert not any(str(row["action"]).startswith("admin.") for row in delta), (
        "settings_success_no_admin_audit"
    )
    _equal(delta, [], "settings_success_no_audit")
    _assert_no_canaries(ctx, json.dumps(rows, ensure_ascii=False), "settings_audit_secrets_absent")


def _assert_refusals(ctx: dict) -> None:
    ctx["storage"].set_permission_override(_ADMIN, "admin.diagnostics", "deny")
    before = _read_state(ctx)
    cases = (
        ({}, 401, "settings_401"),
        (ctx["headers"]["user"], 403, "settings_403_ordinary"),
        (ctx["headers"]["admin"], 403, "settings_403_revoked"),
    )
    for headers, status, code in cases:
        snap, audit_snap = _read_state(ctx), _audits(ctx)
        response = ctx["client"].get(_PATH, headers=headers)
        _equal(response.status_code, status, code)
        _assert_no_canaries(ctx, response.text, "settings_refusal_secrets_absent")
        _equal(_read_state(ctx), snap, "settings_refusal_no_effect")
        rows = _audits(ctx)
        _equal(rows[: len(audit_snap)], audit_snap, "settings_audit_history")
        delta = rows[len(audit_snap) :]
        if status == 401:
            _equal(
                [(row["user_id"], row["action"], row["target_type"], row["target_id"]) for row in delta],
                [("anonymous", "auth.failed", "auth", "invalid_credentials")],
                "settings_401_auth_failed",
            )
        else:
            assert not any(str(row["action"]).startswith("admin.") for row in delta), (
                "settings_403_no_success_audit"
            )
            assert not any(row["action"] == "auth.failed" for row in delta), "settings_403_no_auth_failed"
            _equal(delta, [], "settings_403_no_audit")
        _assert_no_canaries(
            ctx, json.dumps(_audits(ctx), ensure_ascii=False), "settings_audit_secrets_absent"
        )
    _equal(_read_state(ctx), before, "settings_refusal_no_effect")


@pytest.fixture
def settings_http(settings):
    assert not settings.llm_enabled and not settings.workers_enabled
    vault = settings.home.parent / "PRIVATE_SETTINGS_VAULT_CANARY"
    vault.mkdir()
    tuned = replace(
        settings,
        shared_archive=True,
        telegram_owner_chat_ids=[],
        llm_enabled=False,
        workers_enabled=False,
        embeddings_enabled=False,
        secondary_llm_enabled=False,
        llm_api_key=_LLM_KEY,
        embeddings_api_key=_EMB_KEY,
        llm_model=_LLM_MODEL,
        llm_max_tokens=_LLM_MAX_TOKENS,
        embeddings_model=_EMB_MODEL,
        embeddings_base_url=_EMB_URL,
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
            "canaries": (
                _LLM_KEY,
                _EMB_KEY,
                settings.api_token,
                settings.telegram_bridge_secret,
                str(vault),
            ),
            "headers": {},
        }
        for key, person, preset in (
            ("owner", _OWNER, "owner"),
            ("admin", _ADMIN, "admin"),
            ("user", _USER, "user"),
            ("a", _A, "user"),
            ("b", _B, "user"),
        ):
            secret = "jrc_synthetic_ops_" + key
            _issue(storage, person, preset, secret)
            ctx["headers"][key] = {"Authorization": "Bearer " + secret}
        ctx["own"] = _seed_knowledge(storage, _A, _OWN_TITLE, ["own"])
        ctx["foreign"] = _seed_knowledge(storage, _B, _FOREIGN_TITLE, ["foreign"])
        graph = KnowledgeGraph(storage)
        person = graph.create_entity(_A, "Settings Person", EntityType.PERSON)
        box = graph.create_container(_A, "Settings Box", kind="collection")
        ctx["person"] = person["id"]
        ctx["box"] = box["id"]
        yield ctx


def test_owner_and_delegated_settings_http_closed_projection_privacy_and_no_effects(settings_http):
    _assert_success(settings_http, "owner")
    _assert_success(settings_http, "admin")


def test_settings_http_anonymous_ordinary_and_revoked_capability_refusals(settings_http):
    _assert_refusals(settings_http)


_FAULTS = [
    ("missing_effective", "settings_projection_embeddings_effective", "success"),
    ("wrong_auth_flag", "settings_projection_llm_auth", "success"),
    ("secret_in_200", "settings_secrets_absent", "success"),
    ("secret_in_refusal", "settings_refusal_secrets_absent", "refusal"),
    ("secret_in_403", "settings_refusal_secrets_absent", "refusal"),
    ("refusal_audit_prefix", "settings_audit_history", "refusal"),
    ("own_ko", "settings_read_preserves_state", "success"),
    ("refusal_override", "settings_refusal_no_effect", "refusal"),
    ("refusal_link", "settings_refusal_no_effect", "refusal"),
]


def _fault_row(connection, table, row_id, field, value) -> bool:
    found = connection.execute(f"SELECT {field} FROM {table} WHERE id=?", (row_id,)).fetchone()
    return found is not None and found[field] == value


@pytest.mark.parametrize("fault,code,scenario", _FAULTS, ids=[row[0] for row in _FAULTS])
def test_settings_oracles_detect_corrupted_http_and_writes(settings_http, monkeypatch, fault, code, scenario):
    ctx, storage = settings_http, settings_http["storage"]
    original = TestClient.request
    hits: list[str] = []

    def note() -> None:
        hits.append(fault)

    def altered(self, method, url, **kwargs):
        method = method.upper()
        response = original(self, method, url, **kwargs)
        if method != "GET" or url != _PATH:
            return response
        if fault == "missing_effective" and response.status_code == 200:
            body = response.json()
            body["embeddings"].pop("effective", None)
            note()
            return httpx.Response(200, json=body, request=response.request)
        if fault == "wrong_auth_flag" and response.status_code == 200:
            body = response.json()
            body["llm"]["auth"] = False
            note()
            return httpx.Response(200, json=body, request=response.request)
        if fault == "secret_in_200" and response.status_code == 200:
            body = response.json()
            body["llm"]["api_key"] = _LLM_KEY
            note()
            return httpx.Response(200, json=body, request=response.request)
        if (fault == "secret_in_refusal" and response.status_code == 401) or (
            fault == "secret_in_403" and response.status_code == 403
        ):
            payload = response.json()
            payload["detail"] = str(payload.get("detail") or "") + _LLM_KEY
            note()
            return httpx.Response(response.status_code, json=payload, request=response.request)
        if fault == "refusal_audit_prefix" and response.status_code == 403:
            prior = _audits(ctx)
            assert prior, "settings_fault_no_prior_audit"
            row_id = prior[0]["id"]
            # Deliberately bypass the correct append-only guard only inside this
            # synthetic fault transaction; restore it before the oracle reads.
            with storage.transaction() as connection:
                trigger = connection.execute(
                    "SELECT sql FROM sqlite_master WHERE type='trigger' AND name='audit_log_no_update'"
                ).fetchone()
                assert trigger is not None, "settings_fault_no_audit_guard"
                connection.execute("DROP TRIGGER audit_log_no_update")
                try:
                    connection.execute(
                        "UPDATE audit_log SET target_id='CORRUPTED_SETTINGS_PREFIX' WHERE id=?", (row_id,)
                    )
                finally:
                    connection.execute(trigger["sql"])
            found = storage.execute("SELECT target_id FROM audit_log WHERE id=?", (row_id,)).fetchone()
            assert found["target_id"] == "CORRUPTED_SETTINGS_PREFIX", "settings_fault_not_persisted"
            note()
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
        if fault == "refusal_link" and response.status_code == 403 and not ctx.get("_link_injected"):
            ctx["_link_injected"] = True
            KnowledgeGraph(storage).create_relation(_A, ctx["person"], ctx["box"])
            found = storage.execute(
                "SELECT id FROM relations WHERE source_entity_id=? AND target_entity_id=?",
                (ctx["person"], ctx["box"]),
            ).fetchone()
            if found is not None:
                note()
        return response

    monkeypatch.setattr(TestClient, "request", altered)
    with pytest.raises(AssertionError, match=code):
        if scenario == "refusal":
            _assert_refusals(ctx)
        else:
            _assert_success(ctx, "owner")
    assert hits, f"settings_fault_not_injected:{fault}"
