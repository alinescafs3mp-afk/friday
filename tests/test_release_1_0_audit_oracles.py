"""Audit HTTP pages: exact stored rows, stable walks, authority and real faults."""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import replace
from datetime import UTC, datetime

import httpx
import pytest
from fastapi.testclient import TestClient

from friday.permissions import LEGACY_OWNER_USER_ID
from friday.storage.models import AuditEntry, new_id
from tests.test_api_tokens import _issue
from tests.test_release_1_0_knowledge_read_oracles import (
    _audits,
    _read_state,
)
from tests.test_release_1_0_knowledge_read_oracles import (
    knowledge_read_http as knowledge_read_http,
)
from tests.test_release_1_0_knowledge_read_oracles import (
    profile_http as profile_http,
)
from tests.test_release_1_0_profile_oracles import _A, _B

ROOT = "/api/admin/audit"
ADMIN = "local:r10-audit-admin"
ANCHOR = "2021-03-17T09:00:04.000000+00:00"
REQUEST_ID = "r10-audit-reader"


def _equal(actual, expected, code):
    assert actual == expected, (code, actual, expected)


@pytest.fixture
def audit_http(knowledge_read_http):
    ctx = knowledge_read_http
    storage = ctx["storage"]
    for key, person, preset in (("owner", LEGACY_OWNER_USER_ID, "owner"), ("admin", ADMIN, "admin")):
        secret = "jrc_synthetic_audit_oracle_" + key
        _issue(storage, person, preset, secret)
        ctx[key] = {"Authorization": "Bearer " + secret, "X-Request-ID": REQUEST_ID}
    storage.set_permission_override(_A, "admin.audit.read", "deny")
    ctx["seed"] = {}
    seed_ids = sorted(new_id("audit") for _ in range(5))
    key = bytes.fromhex(
        storage.execute("SELECT value FROM schema_meta WHERE key='audit_privacy_hmac_key'").fetchone()[0]
    )
    ctx["request_ref"] = (
        "reqref_" + hmac.new(key, ("request_id\0" + REQUEST_ID).encode(), hashlib.sha256).hexdigest()[:24]
    )
    ctx["missing_ref"] = (
        "audit_log:ref:"
        + hmac.new(key, b"target:audit_log\0local:r10-audit-missing", hashlib.sha256).hexdigest()[:24]
    )
    # Equal timestamps require a stable ID tie-break. Actors and targets differ:
    # filtering the acting account must not accidentally filter target_id.
    for number, person, second in ((1, _A, 1), (2, _B, 2), (3, _A, 3), (4, _A, 3), (5, _B, 4)):
        entry = AuditEntry(
            id=seed_ids[number - 1],
            user_id=person,
            action="admin.knowledge.read",
            target_type="user",
            target_id=_B if person == _A else _A,
            after_json={"count": number},
            created_at=f"2021-03-17T09:00:0{second}.000000+00:00",
        )
        storage.log_audit(entry)
        ctx["seed"][number] = dict(
            storage.execute("SELECT * FROM audit_log WHERE id=?", (entry.id,)).fetchone()
        )
    yield ctx


def _page(ctx, *, params, numbers, total, anchor, role="admin", audit_target=None):
    before, history = _read_state(ctx), _audits(ctx)
    start = datetime.now(UTC).replace(microsecond=0)
    response = ctx["client"].get(ROOT, params=params, headers=ctx[role])
    _equal(response.status_code, 200, "audit_http_status")
    expected = {
        "items": [ctx["seed"][n] for n in numbers],
        "count": len(numbers),
        "total": total,
        "limit": params.get("limit", 500),
        "offset": params.get("offset", 0),
        "anchor": anchor,
    }
    _equal(response.json(), expected, "audit_exact_page")
    _equal(_read_state(ctx), before, "audit_read_business_state")
    rows = _audits(ctx)
    _equal(rows[: len(history)], history, "audit_immutable_prefix")
    added = rows[len(history) :]
    _equal(len(added), 1, "audit_read_one_event")
    row = added[0]
    _equal(
        (
            row["user_id"],
            row["action"],
            row["target_type"],
            row["target_id"],
            row["before_json"],
            json.loads(row["after_json"]),
            row["request_id"],
        ),
        (
            ADMIN if role == "admin" else LEGACY_OWNER_USER_ID,
            "admin.audit.read",
            "audit_log",
            audit_target or params.get("user_id") or "*",
            None,
            {"limit": expected["limit"], "offset": expected["offset"], "returned": len(numbers)},
            ctx["request_ref"],
        ),
        "audit_read_provenance",
    )
    assert row["id"] and row["id"] not in {r["id"] for r in history}, "audit_read_identity"
    assert start <= datetime.fromisoformat(row["created_at"]) <= datetime.now(UTC), "audit_read_clock"
    return response.json()


def _walk(ctx):
    first = _page(
        ctx,
        params={"user_id": _A, "limit": 2},
        numbers=[4, 3],
        total=3,
        anchor="2021-03-17T09:00:03.000000+00:00",
    )
    # A real newer insertion joins the same actor's log between HTTP pages.
    # It and the read's own audit must not shift the anchored snapshot.
    ctx["concurrent_id"] = new_id("audit")
    ctx["storage"].log_audit(
        AuditEntry(
            id=ctx["concurrent_id"],
            user_id=_A,
            action="admin.knowledge.read",
            target_type="user",
            target_id=_B,
        )
    )
    _page(
        ctx,
        params={"user_id": _A, "limit": 2, "offset": 2, "before": first["anchor"]},
        numbers=[1],
        total=3,
        anchor=first["anchor"],
    )
    _page(
        ctx,
        params={"user_id": _A, "limit": 2, "offset": 4, "before": first["anchor"]},
        numbers=[],
        total=3,
        anchor=first["anchor"],
    )
    _page(ctx, params={"before": ANCHOR, "limit": 3}, numbers=[5, 4, 3], total=5, anchor=ANCHOR, role="owner")
    _page(ctx, params={"before": ANCHOR, "limit": 3, "offset": 3}, numbers=[2, 1], total=5, anchor=ANCHOR)
    _page(
        ctx,
        params={"user_id": "local:r10-audit-missing"},
        numbers=[],
        total=0,
        anchor=None,
        audit_target=ctx["missing_ref"],
    )


def test_audit_pages_bind_actor_filter_order_anchor_rows_and_read_provenance(audit_http):
    _walk(audit_http)


def _refused(ctx):
    # Revoke the delegated reader after its token was issued: HTTP must consult
    # current permissions, and refusing the read must not expose stored rows.
    ctx["storage"].set_permission_override(ADMIN, "admin.audit.read", "deny")
    before = _read_state(ctx)
    for role, params, status in (
        (None, {}, 401),
        (_A, {}, 403),
        ("admin", {}, 403),
        ("owner", {"limit": 0}, 422),
        ("owner", {"limit": 5001}, 422),
        ("owner", {"offset": -1}, 422),
        ("owner", {"limit": "invalid"}, 422),
    ):
        history = _audits(ctx)
        response = ctx["client"].get(ROOT, params=params, headers=ctx[role] if role else {})
        _equal(response.status_code, status, "audit_refusal_status")
        _equal(_read_state(ctx), before, "audit_refusal_business_state")
        assert (
            all(row["id"] not in response.text for row in ctx["seed"].values())
            and "items" not in response.json()
        ), "audit_refusal_no_rows"
        after = _audits(ctx)
        _equal(after[: len(history)], history, "audit_immutable_prefix")
        # Security refusals may log their own audit; no successful read event.
        assert not any(r["action"] == "admin.audit.read" for r in after[len(history) :]), (
            "audit_refusal_no_success"
        )


def test_audit_refuses_anonymous_denied_revoked_and_invalid_reads_without_business_effect(audit_http):
    _refused(audit_http)


_FAULTS = [
    ("wrong_actor_row", "audit_exact_page", "walk"),
    ("reversed_tie", "audit_exact_page", "walk"),
    ("wrong_total", "audit_exact_page", "walk"),
    ("ignored_anchor", "audit_exact_page", "walk"),
    ("row_payload", "audit_exact_page", "walk"),
    ("missing_read_audit", "audit_read_one_event", "walk"),
    ("wrong_audit_actor", "audit_read_provenance", "walk"),
    ("wrong_audit_target", "audit_read_provenance", "walk"),
    ("read_business_write", "audit_read_business_state", "walk"),
    ("refusal_permission_write", "audit_refusal_business_state", "refused"),
]


@pytest.mark.parametrize("fault,code,scenario", _FAULTS, ids=[r[0] for r in _FAULTS])
def test_audit_oracles_detect_real_http_storage_and_audit_faults(
    audit_http, monkeypatch, fault, code, scenario
):
    ctx, injected = audit_http, []
    storage = ctx["storage"]
    real_audit = storage.log_audit
    real_request = TestClient.request
    if fault in {"missing_read_audit", "wrong_audit_actor", "wrong_audit_target"}:

        def alter_audit(entry):
            if entry.action == "admin.audit.read":
                injected.append(True)
                if fault == "missing_read_audit":
                    return None
                entry = replace(
                    entry, **({"user_id": _B} if fault == "wrong_audit_actor" else {"target_id": _B})
                )
            return real_audit(entry)

        monkeypatch.setattr(storage, "log_audit", alter_audit)

    def altered(self, method, url, **kwargs):
        response = real_request(self, method, url, **kwargs)
        if injected or method != "GET" or url != ROOT:
            return response
        if fault == "refusal_permission_write" and response.status_code == 403:
            storage.set_permission_override(_B, "admin.audit.read", "allow")
            injected.append(True)
            return response
        if response.status_code != 200:
            return response
        body = response.json()
        if fault == "wrong_actor_row":
            body["items"][0] = ctx["seed"][5]
        elif fault == "reversed_tie":
            body["items"].reverse()
        elif fault == "wrong_total":
            body["total"] += 1
        elif fault == "ignored_anchor" and kwargs["params"].get("offset") == 2:
            body["items"] = [
                dict(
                    storage.execute("SELECT * FROM audit_log WHERE id=?", (ctx["concurrent_id"],)).fetchone()
                )
            ]
        elif fault == "row_payload":
            body["items"][0]["after_json"] = '{"count":999}'
        elif fault == "read_business_write":
            with storage.transaction() as conn:
                conn.execute(
                    "UPDATE knowledge_objects SET content='CORRUPTED_AUDIT_READ' WHERE id=?", (ctx["first"],)
                )
        else:
            return response
        injected.append(True)
        return httpx.Response(200, json=body, request=response.request)

    monkeypatch.setattr(TestClient, "request", altered)
    with pytest.raises(AssertionError, match=code):
        {"walk": _walk, "refused": _refused}[scenario](ctx)
    assert injected, "audit_fault_not_injected"
