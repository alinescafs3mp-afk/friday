"""Real HTTP/storage witness contract over synthetic traces and runtime identity.

These fixtures are not live production observations or a model health proof.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import friday.admin_api._semantic_supervisor as route
import friday.orchestration.supervisor_representative_window_attestation as witness
from friday.orchestration.supervisor_contracts import SupervisorMode
from friday.permissions import LEGACY_OWNER_USER_ID
from friday.server import create_app
from friday.storage.models import RawObject, new_id
from tests.test_api_tokens import _issue
from tests.test_semantic_supervisor_representative_window_attestation import (
    _consume_request,
    _issue_request,
    _predecessor_identity,
    _seed_assist,
    _seed_shadow,
    _Storage,
)

BASE = "/api/admin/semantic-supervisor-witness/"
ISSUE = "issue-representative-window-attestation"
CONSUME = "consume-representative-window-attestation"
TABLES = ("messages", "runtime_events", "raw_objects", "inbox", "knowledge_objects")
STAMP = "2026-01-01T00:00:00+00:00"


def _canonical(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _sha(value):
    return hashlib.sha256(_canonical(value)).hexdigest()


def _rows(store, table):
    assert table in (*TABLES, "request_idempotency", "audit_log")
    return [dict(row) for row in store.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()]


def _business(ctx):
    return {table: _rows(ctx["store"], table) for table in TABLES}


def _records(ctx):
    return [
        row
        for row in _rows(ctx["store"], "request_idempotency")
        if row["request_key"].startswith(witness.REPRESENTATIVE_WINDOW_REQUEST_KEY_PREFIX)
    ]


def _audits(ctx):
    return [
        row
        for row in _rows(ctx["store"], "audit_log")
        if row["action"].startswith("admin.semantic_supervisor.")
    ]


def _state(ctx):
    return _business(ctx), _records(ctx), _audits(ctx)


def _seed_real_storage(store, target):
    """Copy code-owned synthetic trace fixtures into the real application schema."""
    fixture = _Storage()
    try:
        _seed_shadow(fixture)
        if target is SupervisorMode.CANARY:
            _seed_assist(fixture)
        conversation = store.create_conversation(LEGACY_OWNER_USER_ID, "synthetic witness")
        with store.transaction() as connection:
            for row in fixture.conn.execute("SELECT * FROM messages ORDER BY rowid"):
                connection.execute(
                    "INSERT INTO messages(id,conversation_id,user_id,role,content,metadata_json,created_at) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (
                        row["id"],
                        conversation["id"],
                        LEGACY_OWNER_USER_ID,
                        row["role"],
                        row["content"],
                        row["metadata_json"],
                        STAMP,
                    ),
                )
            for row in fixture.conn.execute("SELECT * FROM runtime_events ORDER BY rowid"):
                connection.execute(
                    "INSERT INTO runtime_events(id,event_type,payload,created_at) VALUES(?,?,?,?)",
                    (row["id"], row["event_type"], row["payload"], STAMP),
                )
    finally:
        fixture.conn.close()


@pytest.fixture
def semantic_http(settings, monkeypatch, request):
    target = SupervisorMode(getattr(request, "param", "assist"))
    current = replace(settings, workers_enabled=False)
    app = create_app(current)
    order = []
    control = {"available": True, "identity": _predecessor_identity(target)}

    class Probe:
        async def refresh_semantic_supervisor_runtime_admission(self, *, absolute_deadline_monotonic):
            remaining = absolute_deadline_monotonic - time.monotonic()
            assert 0 < remaining <= witness.REPRESENTATIVE_WINDOW_RUNTIME_REFRESH_SEC
            order.append("refresh")
            return control["available"]

    def identity(settings_arg, scheduler, *, target_mode):
        assert settings_arg is current and isinstance(scheduler, Probe) and target_mode is target
        assert order[-1] == "refresh"
        order.append("identity")
        if not control["available"]:
            raise witness.RepresentativeWindowAttestationError("PRIVATE_RUNTIME_UNAVAILABLE")
        return dict(control["identity"])

    monkeypatch.setattr(route, "representative_window_current_server_identity", identity)
    with TestClient(app, raise_server_exceptions=False) as client:
        app.state.secondary_brain = Probe()
        store = app.state.storage
        headers = {"owner": {"Authorization": f"Bearer {current.api_token}"}, "anonymous": {}}
        for role in ("user", "admin", "scoped-owner"):
            person = LEGACY_OWNER_USER_ID if role == "scoped-owner" else "semantic091-" + role
            secret = "jrc_semantic091_" + role + "_synthetic_secret"
            _issue(store, person, "owner" if role == "scoped-owner" else role, secret)
            headers[role] = {"Authorization": "Bearer " + secret}
        for person in (LEGACY_OWNER_USER_ID, "semantic091-user"):
            store.store_raw_object(
                RawObject(
                    id=new_id("raw"),
                    user_id=person,
                    source="text",
                    source_ref="semantic-witness-fixture",
                    raw_content="PRIVATE_RAW_" + person,
                    content_type="text",
                    received_at=STAMP,
                    created_at=STAMP,
                )
            )
        _seed_real_storage(store, target)
        yield {
            "client": client,
            "store": store,
            "settings": current,
            "headers": headers,
            "target": target,
            "order": order,
            "control": control,
        }


def _post(ctx, endpoint, payload, *, role="owner"):
    return ctx["client"].post(BASE + endpoint, headers=ctx["headers"][role], json=payload)


def _audit_appended(ctx, before, endpoint, response):
    rows = _audits(ctx)
    assert len(rows) == len(before) + 1 and rows[:-1] == before
    row = rows[-1]
    assert row["action"] == "admin.semantic_supervisor." + endpoint.replace("-", "_")
    assert row["user_id"] == LEGACY_OWNER_USER_ID and row["target_id"] is None
    assert row["target_type"] == "semantic_supervisor_witness"
    assert row["before_json"] is None and row["request_id"] == response.headers["x-request-id"]
    text = json.dumps(row)
    assert "PRIVATE" not in text and "synthetic_secret" not in text


def _assert_signature(ctx, document, field, domain, *, omit=()):
    key = bytes.fromhex(
        ctx["store"].execute("SELECT value FROM schema_meta WHERE key='audit_privacy_hmac_key'").fetchone()[0]
    )
    unsigned = {k: v for k, v in document.items() if k not in (field, *omit)}
    assert document[field] == hmac.new(key, domain + _canonical(unsigned), hashlib.sha256).hexdigest()


def _issued(ctx):
    request = _issue_request(ctx["store"], ctx["target"])
    before, audits, rows = _business(ctx), _audits(ctx), _records(ctx)
    order = list(ctx["order"])
    started = int(time.time())
    response = _post(ctx, ISSUE, request)
    assert response.status_code == 200, response.text
    body = response.json()
    assert response.content == _canonical(body)
    assert set(body) == witness.REPRESENTATIVE_WINDOW_ISSUE_RESPONSE_KEYS
    assert body["schema"] == witness.REPRESENTATIVE_WINDOW_ISSUE_RESPONSE_SCHEMA
    assert body["status"] == "unused" and body["state_version"] == 1
    attestation = body["server_attestation"]
    assert set(attestation) == witness.REPRESENTATIVE_WINDOW_ATTESTATION_KEYS
    assert body["server_attestation_sha256"] == _sha(attestation)
    assert re.fullmatch(r"[0-9a-f]{64}", body["attestation_lookup_token"])
    assert (
        body["lookup_token_sha256"]
        == hashlib.sha256(body["attestation_lookup_token"].encode("ascii")).hexdigest()
    )
    assert attestation["lookup_token_sha256"] == body["lookup_token_sha256"]
    assert attestation["target_mode"] == ctx["target"].value
    assert attestation["baseline_report_sha256"] == request["baseline"]["report_sha256"]
    assert attestation["baseline_file_sha256"] == _sha(request["baseline"])
    assert attestation["turn_trace_count"] == (40 if ctx["target"] is SupervisorMode.CANARY else 20)
    assert attestation["joined_trace_count"] == 20
    assert attestation["server_recomputed"] is True and attestation["representative_window_attested"] is True
    for key, value in ctx["control"]["identity"].items():
        assert attestation[key] == value
    assert started <= attestation["issued_at"] <= int(time.time())
    assert (
        attestation["expires_at"]
        == attestation["issued_at"] + witness.REPRESENTATIVE_WINDOW_ATTESTATION_TTL_SEC
    )
    _assert_signature(ctx, attestation, "signature", b"friday.semantic-supervisor-representative-window.v1\0")
    after = _records(ctx)
    assert after[:-1] == rows and len(after) == len(rows) + 1
    stored = json.loads(after[-1]["response_json"])
    expected = {
        **body,
        "attestation_lookup_token": None,
        "consume_state": "unused",
        "consumed_at": None,
        "consume_request_sha256": None,
        "consume_binding_sha256": None,
        "consumed_response_sha256": None,
        "consumed_response": None,
    }
    assert stored == expected and after[-1]["request_hash"] == _sha(request)
    assert after[-1]["user_id"] == LEGACY_OWNER_USER_ID and after[-1]["state"] == "complete"
    assert body["attestation_lookup_token"] not in json.dumps(after)
    assert "PRIVATE" not in response.text and "PRIVATE" not in json.dumps(after)
    assert _business(ctx) == before and ctx["order"] == order + ["refresh", "identity"]
    _audit_appended(ctx, audits, ISSUE, response)
    return body


def _consumed(ctx, issue):
    request = _consume_request(issue)
    before, audits, rows = _business(ctx), _audits(ctx), _records(ctx)
    order = list(ctx["order"])
    response = _post(ctx, CONSUME, request)
    assert response.status_code == 200, response.text
    body = response.json()
    assert (
        response.content == _canonical(body)
        and set(body) == witness.REPRESENTATIVE_WINDOW_CONSUME_RESPONSE_KEYS
    )
    assert body["schema"] == witness.REPRESENTATIVE_WINDOW_CONSUME_RESPONSE_SCHEMA
    assert body["status"] == "consumed" and body["state_version"] == 2
    assert body["server_attestation"] == issue["server_attestation"]
    assert body["consume_request_sha256"] == _sha(request)
    assert body["lookup_token_sha256"] == issue["lookup_token_sha256"]
    assert issue["server_attestation"]["issued_at"] <= body["consumed_at"] <= int(time.time())
    for key in set(request) & set(body) - {"schema"}:
        assert body[key] == request[key]
    _assert_signature(
        ctx,
        body,
        "consume_binding_sha256",
        b"friday.semantic-supervisor-representative-window-consume.v1\0",
        omit=("server_attestation",),
    )
    after = _records(ctx)
    assert len(after) == len(rows) == 1
    assert {k: v for k, v in after[0].items() if k not in ("response_json", "updated_at")} == {
        k: v for k, v in rows[0].items() if k not in ("response_json", "updated_at")
    }
    old = json.loads(rows[0]["response_json"])
    expected = {
        **old,
        "status": "consumed",
        "consume_state": "consumed",
        "consumed_at": body["consumed_at"],
        "consume_request_sha256": _sha(request),
        "consume_binding_sha256": body["consume_binding_sha256"],
        "consumed_response_sha256": _sha(body),
        "consumed_response": body,
        "state_version": 2,
    }
    assert json.loads(after[0]["response_json"]) == expected
    assert issue["attestation_lookup_token"] not in response.text
    assert "PRIVATE" not in response.text and "PRIVATE" not in json.dumps(after)
    assert _business(ctx) == before and ctx["order"] == order + ["refresh", "identity"]
    assert not ctx["store"].conn.in_transaction
    _audit_appended(ctx, audits, CONSUME, response)
    return response.content


@pytest.mark.parametrize("semantic_http", ["assist", "canary"], indirect=True)
def test_semantic_http_issues_recomputes_signs_consumes_and_replays(semantic_http):
    ctx = semantic_http
    issue = _issued(ctx)
    raw = _consumed(ctx, issue)
    stored = _records(ctx)
    assert _consumed(ctx, issue) == raw and _records(ctx) == stored


@pytest.mark.parametrize("damage", ["baseline-hash", "registry", "population-drift", "latency-budget"])
def test_semantic_http_issue_rejects_unbound_or_stale_candidate(semantic_http, damage):
    ctx = semantic_http
    request = _issue_request(ctx["store"], ctx["target"])
    if damage == "baseline-hash":
        request["baseline_file_sha256"] = "0" * 64
    elif damage == "registry":
        request["registry_binding_sha256"] = "0" * 64
    elif damage == "latency-budget":
        request["latency_budget_file_sha256"] = "0" * 64
    else:
        with ctx["store"].transaction() as connection:
            connection.execute("UPDATE messages SET metadata_json='{}' WHERE id='msg_0000000000000000'")
    before = _state(ctx)
    response = _post(ctx, ISSUE, request)
    assert response.status_code == 400, response.text
    assert response.json() == {"detail": "Representative-window candidate не подтверждён сервером"}
    assert _state(ctx) == before and ctx["order"] == ["refresh", "identity"]
    assert not ctx["store"].conn.in_transaction


@pytest.mark.parametrize("damage", ["lookup", "digest", "binding", "expired", "process", "rebind"])
def test_semantic_http_consume_refusal_preserves_exact_witness(semantic_http, monkeypatch, damage):
    ctx = semantic_http
    issue = _issued(ctx)
    request = _consume_request(issue)
    status = 400
    if damage == "rebind":
        _consumed(ctx, issue)
        request["baseline_report_sha256"] = "0" * 64
        status = 409
    elif damage == "lookup":
        request["attestation_lookup_token"] = "0" * 64
    elif damage == "digest":
        request["server_attestation_sha256"] = "0" * 64
    elif damage == "binding":
        request["observer_runner_sha256"] = "0" * 64
    elif damage == "process":
        ctx["control"]["identity"]["primary_process_epoch_sha256"] = "0" * 64
    elif damage == "expired":
        monkeypatch.setattr(witness, "time", SimpleNamespace(time=lambda: 4_000_000_000))
    before = _state(ctx)
    response = _post(ctx, CONSUME, request)
    assert response.status_code == status, response.text
    detail = (
        "Representative-window witness уже использован или изменился"
        if status == 409
        else "Representative-window witness не подтверждён сервером"
    )
    assert response.json() == {"detail": detail} and _state(ctx) == before
    assert not ctx["store"].conn.in_transaction


@pytest.mark.parametrize("endpoint", [ISSUE, CONSUME])
@pytest.mark.parametrize(
    "role,status", [("anonymous", 401), ("user", 403), ("admin", 403), ("scoped-owner", 403)]
)
def test_semantic_http_owner_token_gate_before_services(semantic_http, endpoint, role, status):
    ctx = semantic_http
    before = _state(ctx)
    response = _post(ctx, endpoint, {}, role=role)
    assert response.status_code == status and "detail" in response.json(), response.text
    assert _state(ctx) == before and ctx["order"] == []


@pytest.mark.parametrize("endpoint", [ISSUE, CONSUME])
@pytest.mark.parametrize("body", [b"{", b"[]", b"{}"], ids=["bad-json", "array", "missing-fields"])
def test_semantic_http_malformed_body_before_services(semantic_http, endpoint, body):
    ctx = semantic_http
    before = _state(ctx)
    response = ctx["client"].post(BASE + endpoint, headers=ctx["headers"]["owner"], content=body)
    assert response.status_code == 400 and "detail" in response.json(), response.text
    assert _state(ctx) == before and ctx["order"] == []


@pytest.mark.parametrize("endpoint", [ISSUE, CONSUME])
def test_semantic_http_unavailable_runtime_refuses_after_refresh(semantic_http, endpoint):
    ctx = semantic_http
    request = (
        _issue_request(ctx["store"], ctx["target"]) if endpoint == ISSUE else _consume_request(_issued(ctx))
    )
    before, order = _state(ctx), list(ctx["order"])
    ctx["control"]["available"] = False
    response = _post(ctx, endpoint, request)
    assert response.status_code == 503, response.text
    assert response.json() == {"detail": "Live semantic-supervisor witness сейчас недоступен"}
    assert _state(ctx) == before and ctx["order"] == order + ["refresh", "identity"]
