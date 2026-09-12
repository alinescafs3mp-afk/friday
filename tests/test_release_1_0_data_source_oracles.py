"""Source HTTP contracts with owned real SQLite and actual corruption controls."""

from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
from contextlib import closing
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime

import httpx
import pytest
from fastapi.testclient import TestClient

from friday.permissions import LEGACY_OWNER_USER_ID
from friday.server import create_app
from friday.storage.models import Entity, KnowledgeObject, RawObject
from tests.test_api_tokens import _issue
from tests.test_release_1_0_knowledge_read_oracles import _audits, _read_state

A, B, ADMIN, DENIED = ("local:r10-source-" + s for s in ("a", "b", "admin", "denied"))
ROOT = "/api/admin/data-sources"
ENV, FOREIGN_ENV, ABSENT, VAULT_ENV = (
    "R10_SOURCE_ORACLE_" + s for s in ("OWN", "FOREIGN", "ABSENT", "VAULT")
)
SECRET = "postgresql://test:source-secret-canary@192.0.2.42/vault"
DIRECT_DSN = "private://bad-secret"
SEED_TIME = "2021-03-17T09:00:00+00:00"


def _equal(actual, expected, code):
    assert actual == expected, (code, actual, expected)


def _state(ctx):
    result = _read_state(ctx)
    result["data_sources"] = [
        dict(r) for r in ctx["storage"].execute("SELECT * FROM data_sources ORDER BY user_id,name")
    ]
    result["external_databases"] = {
        name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in ctx["external"].items()
    }
    return result


def _visible(row, *, present):
    return {
        "name": row["name"],
        "kind": row["kind"],
        "dsn_env": row["dsn_env"],
        "description": row["description"],
        "created_at": row["created_at"],
        "last_used_at": row["last_used_at"],
        "secret_present": present,
    }


def _source_audit(ctx, before, expected):
    rows = _audits(ctx)
    _equal(rows[: len(before)], before, "source_audit_history")
    added = [r for r in rows[len(before) :] if r["action"].startswith("admin.data_source")]
    observed = [
        (
            r["user_id"],
            r["action"],
            r["target_type"],
            r["target_id"],
            json.loads(r["after_json"]) if r["after_json"] is not None else None,
        )
        for r in added
    ]
    # Independently bind the persisted opaque source target to the literal
    # requested name; do not call the production sanitizer for the expectation.
    key = bytes.fromhex(
        ctx["storage"]
        .execute("SELECT value FROM schema_meta WHERE key='audit_privacy_hmac_key'")
        .fetchone()[0]
    )
    projected = []
    for actor, action, kind, target, payload in expected:
        if kind == "data_source":
            target = (
                "data_source:ref:"
                + hmac.new(key, ("target:data_source\0" + target).encode(), hashlib.sha256).hexdigest()[:24]
            )
        if action == "admin.data_source.declare":
            payload = {
                "name_chars": len(payload["name"]),
                "kind_chars": len(payload["kind"]),
                "description_chars": len(payload["description"]),
                "created_at": payload["created_at"],
                "private_fields_count": 3,
                "private_chars": len(payload["dsn_env"]),
            }
        elif action == "admin.data_source.schema":
            payload = {"source_chars": len(payload["source"])}
        projected.append((actor, action, kind, target, payload))
    _equal(observed, projected, "source_audit_exact")
    _equal([r["before_json"] for r in added], [None] * len(added), "source_audit_no_before")
    return rows


def _private(ctx, *bodies):
    observed = json.dumps([*bodies, _audits(ctx), _state(ctx)["data_sources"]], ensure_ascii=False)
    for token in (
        DIRECT_DSN,
        SECRET,
        "source-secret-canary",
        "192.0.2.42",
        *(str(p) for p in ctx["external"].values()),
    ):
        assert token not in observed, "source_connection_secret"


@pytest.fixture
def source_http(settings, monkeypatch, tmp_path):
    assert not settings.llm_enabled and not settings.workers_enabled
    external = {
        "own": tmp_path / "own-source-private.sqlite",
        "foreign": tmp_path / "foreign-source-private.sqlite",
    }
    for name, path in external.items():
        with closing(sqlite3.connect(path)) as conn, conn:
            if name == "own":
                conn.execute("CREATE TABLE staff (id INTEGER PRIMARY KEY, unit TEXT NOT NULL)")
                conn.execute("INSERT INTO staff(unit) VALUES ('склад')")
                conn.execute("CREATE TABLE stock (quantity REAL)")
                conn.execute("INSERT INTO stock VALUES (12.5)")
            else:
                conn.execute("CREATE TABLE foreign_ledger (private_note TEXT)")
                conn.execute("INSERT INTO foreign_ledger VALUES ('FOREIGN_PRIVATE_SOURCE')")
    monkeypatch.setenv(ENV, str(external["own"]))
    monkeypatch.setenv(FOREIGN_ENV, str(external["foreign"]))
    monkeypatch.setenv(VAULT_ENV, SECRET)
    monkeypatch.delenv(ABSENT, raising=False)
    app = create_app(replace(settings, shared_archive=False, telegram_owner_chat_ids=[]))
    with TestClient(app) as client:
        storage = app.state.storage
        ctx = {"client": client, "storage": storage, "external": external, "headers": {}}
        # Scoped owner credential: only its credential activity is touched by
        # HTTP auth; no legacy-owner user-clock allowance is needed here.
        for key, person, preset in (
            ("owner", LEGACY_OWNER_USER_ID, "owner"),
            ("a", A, "user"),
            ("b", B, "user"),
            ("admin", ADMIN, "admin"),
            ("denied", DENIED, "user"),
        ):
            secret = "jrc_synthetic_source_oracle_" + key
            _issue(storage, person, preset, secret)
            ctx["headers"][key] = {"Authorization": "Bearer " + secret}
        storage.set_permission_override(DENIED, "data.read", "deny")
        storage.set_permission_override(A, "data.read", "allow")
        storage.set_permission_override(A, "admin.all_data.manage", "allow")
        for person in (A, B):
            rid = "raw-source-oracle-" + person
            storage.store_raw_object(RawObject(rid, person, "test", rid, "seed body", "text"))
            storage.store_knowledge_object(
                KnowledgeObject(
                    id="ko-source-oracle-" + person,
                    user_id=person,
                    raw_object_id=rid,
                    content="seed knowledge",
                )
            )
            storage.create_entity(Entity("entity-source-oracle-" + person, person, "Seed entity"))
        for person, name, kind, env, description in (
            (A, "hr", "sqlite", ENV, "Кадры"),
            (A, "absent", "sqlite", ABSENT, "Будущий источник"),
            (A, "vault", "postgres", VAULT_ENV, "Хранилище"),
            (B, "hr", "sqlite", FOREIGN_ENV, "Чужие кадры"),
            (B, "billing", "sqlite", FOREIGN_ENV, "Чужой биллинг"),
        ):
            storage.register_data_source(
                person, name=name, kind=kind, dsn_env=env, description=description, created_by=person
            )
        storage.execute("UPDATE data_sources SET created_at=?", (SEED_TIME,))
        storage.commit()
        yield ctx


def _call(ctx, method, path=ROOT, *, body=None, target=A, role="owner", status=200):
    kwargs = {"headers": ctx["headers"][role] if role else {}}
    if method == "POST":
        kwargs["json"] = {**(body or {}), "user_id": target}
    else:
        kwargs["params"] = {"user_id": target}
    response = ctx["client"].request(method, path, **kwargs)
    _equal(response.status_code, status, "source_http_status")
    return response.json()


def _read(ctx):
    before, audits = _state(ctx), _audits(ctx)
    selected = [r for r in before["data_sources"] if r["user_id"] == A]
    body = _call(ctx, "GET")
    _equal(
        body,
        {"user_id": A, "sources": [_visible(r, present=r["name"] != "absent") for r in selected]},
        "source_exact_list",
    )
    own_audit = (LEGACY_OWNER_USER_ID, "admin.data_sources.read", "user", A, None)
    audits = _source_audit(ctx, audits, [own_audit])
    _private(ctx, body)
    _equal(_state(ctx), before, "source_read_state")
    # Default target selection uses the authenticated account, never the owner.
    response = ctx["client"].get(ROOT, headers=ctx["headers"]["a"])
    _equal(response.status_code, 200, "source_http_status")
    _equal(response.json(), body, "source_default_person")
    audits = _source_audit(ctx, audits, [])
    schema = _call(ctx, "GET", ROOT + "/hr/schema")
    _equal(
        schema,
        {
            "source": "hr",
            "kind": "sqlite",
            "tables": {
                "staff": [{"column": "id", "type": "INTEGER"}, {"column": "unit", "type": "TEXT"}],
                "stock": [{"column": "quantity", "type": "REAL"}],
            },
            "truncated": False,
        },
        "source_exact_schema",
    )
    audits = _source_audit(
        ctx, audits, [(LEGACY_OWNER_USER_ID, "admin.data_source.schema", "user", A, {"source": "hr"})]
    )
    _equal(_state(ctx), before, "source_read_state")
    absent = _call(ctx, "GET", ROOT + "/absent/schema", status=409)
    _equal(absent, {"detail": f"Переменная {ABSENT} не задана — подключаться нечем"}, "source_absent_env")
    missing = _call(ctx, "GET", ROOT + "/missing/schema", status=404)
    _equal(missing, {"detail": "Такой источник не объявлен"}, "source_missing")
    _source_audit(
        ctx,
        audits,
        [
            (LEGACY_OWNER_USER_ID, "admin.data_source.schema", "user", A, {"source": n})
            for n in ("absent", "missing")
        ],
    )
    _equal(_state(ctx), before, "source_read_state")
    _private(ctx, body, schema, absent, missing)


def test_source_reads_bind_exact_person_schema_and_audit_without_effects(source_http):
    _read(source_http)


def _write(ctx):
    before, audits = _state(ctx), _audits(ctx)
    declared = {"name": "billing", "kind": "sqlite", "dsn_env": ABSENT, "description": "Биллинг склада"}
    start = datetime.now(UTC).replace(microsecond=0)
    # A creates its own source with explicit management authority. A different
    # owner actor later replaces/forgets it; the original creator must survive.
    body = _call(ctx, "POST", body=declared, role="a")
    assert isinstance(body.get("created_at"), str), "source_created_time"
    assert start <= datetime.fromisoformat(body["created_at"]) <= datetime.now(UTC), "source_created_time"
    stored = {
        **declared,
        "user_id": A,
        "created_at": body["created_at"],
        "created_by": A,
        "last_used_at": None,
    }
    expected = deepcopy(before)
    expected["data_sources"] = sorted(
        before["data_sources"] + [stored], key=lambda r: (r["user_id"], r["name"])
    )
    _equal(_state(ctx), expected, "source_create_closed_effect")
    visible = _visible(stored, present=False)
    _equal(body, visible, "source_created_response")
    audits = _source_audit(ctx, audits, [(A, "admin.data_source.declare", "data_source", "billing", visible)])
    _private(ctx, body)
    # Upsert preserves the original creator/time and every sibling's whole row.
    changed = {**declared, "kind": "postgres", "dsn_env": VAULT_ENV, "description": "Новый биллинг"}
    body = _call(ctx, "POST", body=changed)
    revised = {**stored, **changed}
    expected["data_sources"] = [
        revised if (r["user_id"], r["name"]) == (A, "billing") else r for r in expected["data_sources"]
    ]
    _equal(_state(ctx), expected, "source_replace_closed_effect")
    visible = _visible(revised, present=True)
    _equal(body, visible, "source_replace_response")
    audits = _source_audit(
        ctx, audits, [(LEGACY_OWNER_USER_ID, "admin.data_source.declare", "data_source", "billing", visible)]
    )
    _private(ctx, body)
    body = _call(ctx, "DELETE", ROOT + "/billing")
    _equal(body, {"status": "forgotten", "name": "billing"}, "source_forget_response")
    _equal(_state(ctx), before, "source_forget_closed_effect")
    audits = _source_audit(
        ctx, audits, [(LEGACY_OWNER_USER_ID, "admin.data_source.forget", "data_source", "billing", None)]
    )
    # Retrying removal cannot delete the other person's same-named source.
    body = _call(ctx, "DELETE", ROOT + "/billing", status=404)
    _equal(body, {"detail": "Такой источник не объявлен"}, "source_missing")
    _source_audit(ctx, audits, [])
    _equal(_state(ctx), before, "source_forget_closed_effect")
    _private(ctx, body)


def test_source_declare_upsert_and_forget_have_closed_row_effects_and_audit(source_http):
    _write(source_http)


def _refusal(ctx):
    before, audits = _state(ctx), _audits(ctx)
    declared = {"name": "hr", "kind": "sqlite", "dsn_env": ENV, "description": "Кадры"}
    routes = [("GET", ROOT), ("GET", ROOT + "/hr/schema"), ("POST", ROOT), ("DELETE", ROOT + "/hr")]
    for role, status in ((None, 401), ("denied", 403)):
        for method, path in routes:
            body = _call(ctx, method, path, role=role, status=status, body=declared)
            _private(ctx, body)
            _equal(_state(ctx), before, "source_refusal_no_effect")
            audits = _source_audit(ctx, audits, [])
    for method, path in routes[2:]:
        body = _call(ctx, method, path, role="admin", status=403, target=LEGACY_OWNER_USER_ID, body=declared)
        _private(ctx, body)
        _equal(_state(ctx), before, "source_owner_protection")
        audits = _source_audit(ctx, audits, [])
    for field, value, reason in (
        ("name", "Кадры HR", "Имя источника"),
        ("kind", "oracle", "вид источника"),
        ("dsn_env", DIRECT_DSN, "переменной окружения"),
    ):
        body = _call(ctx, "POST", body={**declared, field: value}, status=400)
        _private(ctx, body)
        if field == "dsn_env":
            _equal(
                body,
                {"detail": "Имя переменной окружения: заглавные буквы, цифры, подчёркивание"},
                "source_invalid_closed_body",
            )
        assert reason in body["detail"], "source_invalid_reason"
        _equal(_state(ctx), before, "source_invalid_no_effect")
        audits = _source_audit(ctx, audits, [])
        _private(ctx, body)


def test_source_authority_and_invalid_declarations_preserve_business_state(source_http):
    _refusal(source_http)


_FAULTS = [
    ("list_foreign", "source_exact_list", "read"),
    ("schema_columns", "source_exact_schema", "read"),
    ("schema_db_write", "source_read_state", "read"),
    ("read_audit", "source_audit_exact", "read"),
    ("created_by", "source_create_closed_effect", "write"),
    ("replace_clock", "source_replace_closed_effect", "write"),
    ("foreign_delete", "source_forget_closed_effect", "write"),
    ("declare_audit", "source_audit_exact", "write"),
    ("audit_target", "source_audit_exact", "write"),
    ("declare_secret", "source_created_response", "write"),
    ("invalid_reflection", "source_connection_secret", "refusal"),
    ("anonymous_reflection", "source_connection_secret", "refusal"),
    ("denied_reflection", "source_connection_secret", "refusal"),
]


@pytest.mark.parametrize("fault,code,scenario", _FAULTS, ids=[r[0] for r in _FAULTS])
def test_source_oracles_reject_actual_response_storage_database_and_audit_faults(
    source_http, monkeypatch, fault, code, scenario
):
    ctx, storage = source_http, source_http["storage"]
    injected = []
    original = TestClient.request
    if fault.endswith("audit") or fault == "audit_target":
        real_audit = storage.log_audit
        action = "admin.data_sources.read" if fault == "read_audit" else "admin.data_source.declare"

        def omit(entry):
            if entry.action == action:
                injected.append(True)
                if fault == "audit_target":
                    return real_audit(replace(entry, target_id="other-source"))
                return None
            return real_audit(entry)

        monkeypatch.setattr(storage, "log_audit", omit)

    def altered(self, method, url, **kwargs):
        response = original(self, method, url, **kwargs)
        if injected:
            return response
        if (
            (
                fault == "invalid_reflection"
                and response.status_code == 400
                and (kwargs.get("json") or {}).get("dsn_env") == DIRECT_DSN
            )
            or (fault == "anonymous_reflection" and response.status_code == 401)
            or (fault == "denied_reflection" and response.status_code == 403)
        ):
            body = response.json()
            body["detail"] += " [" + DIRECT_DSN + "]"
            injected.append(True)
            return httpx.Response(response.status_code, json=body, request=response.request)
        if response.status_code != 200 or fault.endswith("audit"):
            return response
        body = response.json()
        if fault == "list_foreign" and method == "GET" and url == ROOT:
            body["sources"][0] = _visible(storage.get_data_source(B, "billing"), present=True)
        elif (
            fault in {"schema_columns", "schema_db_write"} and method == "GET" and url.endswith("/hr/schema")
        ):
            if fault == "schema_columns":
                body["tables"]["staff"][0]["column"] = "wrong_column"
            else:
                with closing(sqlite3.connect(ctx["external"]["own"])) as conn, conn:
                    conn.execute("UPDATE stock SET quantity=999")
        elif fault in {"created_by", "declare_secret"} and method == "POST" and url == ROOT:
            if fault == "declare_secret":
                body["dsn"] = SECRET
            else:
                with storage.transaction() as conn:
                    conn.execute(
                        "UPDATE data_sources SET created_by=? WHERE user_id=? AND name='billing'", (B, A)
                    )
        elif fault == "replace_clock" and method == "POST" and kwargs["json"]["kind"] == "postgres":
            with storage.transaction() as conn:
                conn.execute(
                    "UPDATE data_sources SET created_at=? WHERE user_id=? AND name='billing'", (SEED_TIME, A)
                )
        elif fault == "foreign_delete" and method == "DELETE" and url == ROOT + "/billing":
            storage.forget_data_source(B, "billing")
        else:
            return response
        injected.append(True)
        return httpx.Response(200, json=body, request=response.request)

    monkeypatch.setattr(TestClient, "request", altered)
    with pytest.raises(AssertionError, match=code):
        {"read": _read, "write": _write, "refusal": _refusal}[scenario](ctx)
    assert injected, "source_fault_not_injected"
