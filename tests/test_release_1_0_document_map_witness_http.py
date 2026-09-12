"""HTTP document-map witness with real durable effects and scripted runtime identity.

The scheduler attempt and installed-release identity are fixture boundaries. This
does not certify a live model, an installed release, or concurrent transactions.
"""

from __future__ import annotations

import hashlib
import json
import stat
import time
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

import friday.secondary_brain.document_map_evidence as evidence
from friday.permissions import LEGACY_OWNER_USER_ID
from friday.secondary_brain.contracts import ModelWorkload, SecondaryAttempt
from friday.secondary_product_witness import secondary_product_canonical, secondary_product_sha256
from friday.server import create_app
from friday.storage import init_storage
from friday.storage.models import RawObject, new_id
from tests.test_api_tokens import _issue
from tests.test_secondary_document_map_evidence import (
    _RESULT_SENTINEL,
    _consume_request,
    _identity,
    _one_shot_scheduler,
    _result,
    _shadow_settings,
)

BASE = "/api/admin/secondary-document-map-witness/"
OBSERVE = BASE + "observe-shadow"
CONSUME = BASE + "consume-rollout-attestation"
PREFIX = "secondary-document-map-shadow"
TABLES = ("raw_objects", "inbox", "knowledge_objects", "file_source_aliases", "feedback", "feedback_state")


def _rows(store, table):
    assert table in (*TABLES, "audit_log", "request_idempotency")
    return [dict(row) for row in store.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()]


def _business(store):
    return {table: _rows(store, table) for table in TABLES}


def _witness_rows(store):
    return [row for row in _rows(store, "request_idempotency") if row["request_key"].startswith(PREFIX)]


def _witness_audits(store):
    return [row for row in _rows(store, "audit_log") if "document_map" in row["action"]]


def _receipt_bytes(ctx):
    path = ctx["settings"].state_dir / evidence.DOCUMENT_MAP_SHADOW_RECEIPT_FILENAME
    return path.read_bytes() if path.exists() else None


def _snapshot(ctx):
    return (
        _business(ctx["store"]),
        _witness_rows(ctx["store"]),
        _witness_audits(ctx["store"]),
        _receipt_bytes(ctx),
    )


@pytest.fixture
def document_http(settings, monkeypatch):
    current = _shadow_settings(replace(settings, workers_enabled=False))
    scheduler = _one_shot_scheduler(monkeypatch)
    calls = []
    identities = []
    identity = _identity()

    async def attempt(request, *, shadow=False):
        assert shadow is True and request.workload is ModelWorkload.DOCUMENT_MAP
        assert request.max_output_tokens == 256 and request.contains_private_text is True
        assert request.require_structured_output and request.require_independent_model
        assert len(request.messages) == 2
        assert "code-owned inert sample" in request.messages[1]["content"]
        calls.append(request)
        scheduler._selected_by_workload[request.workload] += 1  # noqa: SLF001
        return SecondaryAttempt.success(_result())

    def installed_identity(*args, **kwargs):
        assert kwargs == {"verify_release_tree": True}
        identities.append(dict(identity))
        return dict(identity)

    monkeypatch.setattr(scheduler, "_attempt_unobserved", attempt)
    monkeypatch.setattr(evidence, "_server_identity", installed_identity)
    app = create_app(current)
    with TestClient(app, raise_server_exceptions=False) as client:
        app.state.secondary_brain = scheduler
        store = app.state.storage
        headers = {"owner": {"Authorization": f"Bearer {current.api_token}"}, "anonymous": {}}
        for role in ("user", "admin", "scoped-owner"):
            secret = f"jrc_document090_{role}_synthetic_secret"
            person = LEGACY_OWNER_USER_ID if role == "scoped-owner" else f"document090-{role}"
            _issue(store, person, "owner" if role == "scoped-owner" else role, secret)
            headers[role] = {"Authorization": f"Bearer {secret}"}
        for person in (LEGACY_OWNER_USER_ID, "document090-user"):
            store.store_raw_object(
                RawObject(
                    id=new_id("raw"),
                    user_id=person,
                    source="text",
                    source_ref="document090-foreign-control",
                    raw_content="PRIVATE_RAW_DOCUMENT_" + person,
                    content_type="text",
                    received_at="2026-01-01T00:00:00+00:00",
                    created_at="2026-01-01T00:00:00+00:00",
                )
            )
        yield {
            "client": client,
            "store": store,
            "settings": current,
            "headers": headers,
            "calls": calls,
            "identities": identities,
            "identity": identity,
        }


def _audit_appended(ctx, before, action):
    rows = _witness_audits(ctx["store"])
    assert rows[:-1] == before and len(rows) == len(before) + 1
    row = rows[-1]
    assert row["action"] == "admin.inbox." + action
    assert row["user_id"] == LEGACY_OWNER_USER_ID
    assert row["target_type"] == "inbox" and row["target_id"] is None
    text = json.dumps(row, ensure_ascii=False)
    for secret in (_RESULT_SENTINEL, "PRIVATE_RAW_DOCUMENT_", "synthetic_secret"):
        assert secret not in text


def _observe(ctx):
    before = _business(ctx["store"])
    audits = _witness_audits(ctx["store"])
    start = int(time.time())
    response = ctx["client"].post(OBSERVE, headers=ctx["headers"]["owner"], content=b"")
    assert response.status_code == 200, response.text
    body = response.json()
    raw = _receipt_bytes(ctx)
    assert raw is not None
    receipt = json.loads(raw)
    assert raw == secondary_product_canonical(receipt)
    assert (
        stat.S_IMODE(
            (ctx["settings"].state_dir / evidence.DOCUMENT_MAP_SHADOW_RECEIPT_FILENAME).stat().st_mode
        )
        == 0o600
    )
    expected = {
        "schema": evidence.DOCUMENT_MAP_SHADOW_ONE_SHOT_RESPONSE_SCHEMA,
        "status": "passed",
        "workload": "document_map",
        "routing_mode": "shadow",
        "primary_invocations": 1,
        "primary_result_preserved": True,
        "secondary_result_discarded": True,
        "observation_kind": "exclusive_owner_one_shot",
        "scheduler_selected_delta": 1,
        "scheduler_success_delta": 1,
        "shadow_valid_delta": 1,
        "shadow_invalid_delta": 0,
        "shadow_skipped_delta": 0,
        "shadow_in_flight_before": 0,
        "shadow_in_flight_after": 0,
        "receipt_sha256": hashlib.sha256(raw).hexdigest(),
        "server_rollout_attestation_sha256": secondary_product_sha256(receipt["server_rollout_attestation"]),
        "document_text_retained_in_evidence": False,
        "model_response_retained_in_evidence": False,
        "document_text_digest_retained_in_evidence": False,
        "model_response_digest_retained_in_evidence": False,
    }
    assert body == expected and response.content == secondary_product_canonical(expected)
    assert len(ctx["calls"]) == 1 and len(ctx["identities"]) == 2
    rows = _witness_rows(ctx["store"])
    assert len(rows) == 2 and all(row["user_id"] == LEGACY_OWNER_USER_ID for row in rows)
    latch = next(json.loads(row["response_json"]) for row in rows if "-one-shot:" in row["request_key"])
    assert latch["status"] == "passed" and latch["state_version"] == 1
    assert latch["receipt_sha256"] == expected["receipt_sha256"]
    assert start <= latch["started_at"] <= latch["completed_at"] <= int(time.time())
    _audit_appended(ctx, audits, "observe_secondary_document_map_shadow")
    assert _business(ctx["store"]) == before
    for private in (
        _RESULT_SENTINEL,
        hashlib.sha256(_RESULT_SENTINEL.encode()).hexdigest(),
        "PRIVATE_RAW_DOCUMENT_",
    ):
        assert private.encode() not in raw and private not in response.text
        assert private not in json.dumps(rows)
    return _consume_request(receipt, body["receipt_sha256"])


def _consume(ctx, request):
    audits = _witness_audits(ctx["store"])
    before = _business(ctx["store"])
    receipt = _receipt_bytes(ctx)
    response = ctx["client"].post(CONSUME, headers=ctx["headers"]["owner"], json=request)
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == evidence.DOCUMENT_MAP_SHADOW_CONSUME_RESPONSE_KEYS
    assert body["schema"] == evidence.DOCUMENT_MAP_SHADOW_CONSUME_RESPONSE_SCHEMA
    assert body["status"] == "consumed" and body["state_version"] == 2
    assert body["request_sha256"] == secondary_product_sha256(request)
    assert body["lookup_token_sha256"] == secondary_product_sha256(request["attestation_lookup_token"])
    assert isinstance(body["consumed_at"], int) and body["consumed_at"] <= int(time.time())
    for key in set(request) & set(body) - {"schema"}:
        assert body[key] == request[key]
    assert len(body["consume_binding_sha256"]) == 64
    assert response.content == secondary_product_canonical(body)
    rows = _witness_rows(ctx["store"])
    latch = next(json.loads(row["response_json"]) for row in rows if "-one-shot:" in row["request_key"])
    stored = next(json.loads(row["response_json"]) for row in rows if "-one-shot:" not in row["request_key"])
    assert len(rows) == 2 and latch["status"] == stored["rollout_consume_state"] == "consumed"
    assert (
        latch["consume_request_sha256"] == stored["rollout_consume_request_sha256"] == body["request_sha256"]
    )
    assert stored["rollout_consume_binding_sha256"] == body["consume_binding_sha256"]
    assert latch["consumed_at"] == stored["rollout_consumed_at"] == body["consumed_at"]
    assert latch["state_version"] == stored["rollout_state_version"] == 2
    assert not ctx["store"].conn.in_transaction
    _audit_appended(ctx, audits, "consume_secondary_document_map_rollout_attestation")
    assert _business(ctx["store"]) == before and _receipt_bytes(ctx) == receipt
    assert len(ctx["calls"]) == 1
    assert request["attestation_lookup_token"] not in response.text
    return response.content


def test_document_map_http_issue_consume_and_durable_exact_replay(document_http):
    ctx = document_http
    request = _observe(ctx)
    before = _snapshot(ctx)
    replay = ctx["client"].post(OBSERVE, headers=ctx["headers"]["owner"], content=b"")
    assert replay.status_code == 409 and _snapshot(ctx) == before and len(ctx["calls"]) == 1
    raw = _consume(ctx, request)
    durable = _witness_rows(ctx["store"])
    assert _consume(ctx, request) == raw and _witness_rows(ctx["store"]) == durable
    restarted = init_storage(ctx["settings"])
    try:
        assert _witness_rows(restarted) == durable
    finally:
        restarted.close()
    assert _receipt_bytes(ctx) is not None


@pytest.mark.parametrize("damage", ["lookup", "attestation", "receipt", "process", "expired", "rebind"])
def test_document_map_http_consume_refusal_preserves_witness(document_http, monkeypatch, damage):
    ctx = document_http
    request = _observe(ctx)
    status = 400
    if damage == "rebind":
        _consume(ctx, request)
        request = {**request, "candidate_commit": "8" * 40}
        status = 409
    elif damage == "lookup":
        request = {**request, "attestation_lookup_token": "0" * 64}
    elif damage == "attestation":
        request = {**request, "server_rollout_attestation_sha256": "0" * 64}
    elif damage == "receipt":
        request = {**request, "product_receipt_sha256": "0" * 64, "accepted_shadow_receipt_sha256": "0" * 64}
    elif damage == "process":
        ctx["identity"]["primary_process_epoch_sha256"] = "0" * 64
    elif damage == "expired":
        clock = type("WitnessClock", (), {"time": staticmethod(lambda: 4_000_000_000)})
        monkeypatch.setattr(evidence, "time", clock)
    before = _snapshot(ctx)
    response = ctx["client"].post(CONSUME, headers=ctx["headers"]["owner"], json=request)
    assert response.status_code == status, response.text
    detail = (
        "Подтверждение document-map shadow уже использовано или изменилось"
        if status == 409
        else "Некорректное подтверждение document-map shadow"
    )
    assert response.json() == {"detail": detail}
    assert _snapshot(ctx) == before and len(ctx["calls"]) == 1
    assert not ctx["store"].conn.in_transaction


@pytest.mark.parametrize("endpoint", ["observe-shadow", "consume-rollout-attestation"])
@pytest.mark.parametrize(
    "role,status", [("anonymous", 401), ("user", 403), ("admin", 403), ("scoped-owner", 403)]
)
def test_document_map_http_owner_token_guard_before_witness(document_http, endpoint, role, status):
    ctx = document_http
    before = _snapshot(ctx)
    response = ctx["client"].post(BASE + endpoint, headers=ctx["headers"][role], content=b"")
    assert response.status_code == status, response.text
    assert "detail" in response.json() and "receipt_sha256" not in response.json()
    assert _snapshot(ctx) == before and ctx["calls"] == ctx["identities"] == []


@pytest.mark.parametrize(
    "body", [b"{}", b"null", b" ", b'{"text":"PRIVATE_REQUEST"}'], ids=["object", "null", "space", "text"]
)
def test_document_map_http_observe_body_refused_before_witness(document_http, body):
    ctx = document_http
    before = _snapshot(ctx)
    response = ctx["client"].post(OBSERVE, headers=ctx["headers"]["owner"], content=body)
    assert response.status_code == 400 and response.json() == {
        "detail": "Document-map witness не принимает тело запроса"
    }
    assert _snapshot(ctx) == before and ctx["calls"] == ctx["identities"] == []


@pytest.mark.parametrize("body", [b"{", b"[]", b"{}"], ids=["invalid-json", "array", "missing-fields"])
def test_document_map_http_consume_bad_body_before_witness(document_http, body):
    ctx = document_http
    before = _snapshot(ctx)
    response = ctx["client"].post(CONSUME, headers=ctx["headers"]["owner"], content=body)
    assert response.status_code == 400 and "detail" in response.json(), response.text
    assert _snapshot(ctx) == before and ctx["calls"] == ctx["identities"] == []


def test_document_map_http_failed_receipt_burns_attempt(document_http, monkeypatch):
    ctx = document_http
    product = _business(ctx["store"])
    audits = _witness_audits(ctx["store"])

    def disk_failed(*args, **kwargs):
        raise RuntimeError("PRIVATE_RECEIPT_FAILURE")

    monkeypatch.setattr(evidence, "_write_private_receipt", disk_failed)
    response = ctx["client"].post(OBSERVE, headers=ctx["headers"]["owner"], content=b"")
    assert response.status_code == 503, response.text
    assert response.json() == {
        "detail": "Document-map shadow observation не дало promotion-grade подтверждение"
    }
    assert _receipt_bytes(ctx) is None and len(ctx["calls"]) == 1
    rows = _witness_rows(ctx["store"])
    latch = next(json.loads(row["response_json"]) for row in rows if "-one-shot:" in row["request_key"])
    assert latch["status"] == "failed" and latch["receipt_sha256"] == ""
    before = _snapshot(ctx)
    retry = ctx["client"].post(OBSERVE, headers=ctx["headers"]["owner"], content=b"")
    assert retry.status_code == 409 and len(ctx["calls"]) == 1 and _snapshot(ctx) == before
    assert _business(ctx["store"]) == product and _witness_audits(ctx["store"]) == audits
