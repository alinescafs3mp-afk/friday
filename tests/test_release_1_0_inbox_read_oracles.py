"""Personal Inbox HTTP: structural self cards, selected admin content and boundaries."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime

import httpx
import pytest
from fastapi.testclient import TestClient

from friday.knowledge_graph import KnowledgeGraph
from friday.permissions import LEGACY_OWNER_USER_ID
from friday.storage.models import EntityType
from tests.test_api_tokens import _issue
from tests.test_organs_profile_chronicle import _seed_knowledge
from tests.test_release_1_0_knowledge_read_oracles import _ADMIN, _audits, _read_state
from tests.test_release_1_0_knowledge_read_oracles import knowledge_read_http as knowledge_read_http
from tests.test_release_1_0_knowledge_read_oracles import profile_http as profile_http
from tests.test_release_1_0_profile_oracles import _A, _B, _GUEST

SELF, ADMIN = "/api/inbox", "/api/admin/inbox"
STAMP = "2024-03-01T00:00:00+00:00"
PRIVATE = "PRIVATE_INBOX_BODY_049"
NOTES = "PRIVATE_REVIEWER_NOTES_049"
pytestmark = pytest.mark.parametrize("profile_http", [False], indirect=True, ids=["personal"])


def _equal(actual, expected, code):
    assert actual == expected, (code, actual, expected)


def _state(ctx):
    rows = _read_state(ctx)
    rows["private_entity_owners"] = [
        dict(r) for r in ctx["storage"].execute("SELECT * FROM private_entity_owners ORDER BY rowid")
    ]
    return rows


@pytest.fixture
def inbox_http(knowledge_read_http):
    ctx, storage = knowledge_read_http, knowledge_read_http["storage"]
    owner = "jrc_synthetic_inbox_owner"
    _issue(storage, LEGACY_OWNER_USER_ID, "owner", owner)
    ctx["owner"] = {"Authorization": "Bearer " + owner}
    storage.set_permission_override(_A, "admin.all_data.read", "allow")
    storage.set_permission_override(_B, "admin.all_data.read", "deny")
    storage.set_permission_override(_B, "inbox.read", "deny")
    hidden = _seed_knowledge(storage, _A, PRIVATE, [])
    graph = KnowledgeGraph(storage)
    entity = graph.create_entity(_A, "Private Inbox Entity", EntityType.PERSON)
    graph.link_knowledge_to_entity(hidden, entity["id"], _A, status="accepted", reviewed_by=_A)
    ctx["inbox_seed"], ctx["raw_seed"], ctx["ko_seed"] = {}, {}, {}
    keys = (ctx["first"], ctx["second"], ctx["undated"], hidden, ctx["foreign"])
    with storage.transaction() as conn:
        for number, key in enumerate(keys):
            ko = dict(conn.execute("SELECT * FROM knowledge_objects WHERE id=?", (key,)).fetchone())
            raw = dict(
                conn.execute("SELECT * FROM raw_objects WHERE id=?", (ko["raw_object_id"],)).fetchone()
            )
            iid = f"inbox_r10_read_{number}"
            conn.execute(
                "INSERT INTO inbox(id,user_id,raw_object_id,knowledge_object_id,status,"
                "created_at,classification_notes,suggestions_json,suggested_tags_json,"
                "promotion_score,quality_score) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    iid,
                    ko["user_id"],
                    raw["id"],
                    key,
                    "classified" if number == 2 else "pending",
                    STAMP,
                    NOTES,
                    '{"note":"advisory"}',
                    '["tag"]',
                    0.75,
                    0.5,
                ),
            )
            ctx["inbox_seed"][iid] = dict(conn.execute("SELECT * FROM inbox WHERE id=?", (iid,)).fetchone())
            ctx["raw_seed"][iid], ctx["ko_seed"][iid] = raw, ko
        conn.execute(
            "INSERT INTO private_entity_owners(entity_id,person_id,privacy_kind,created_at) "
            "VALUES(?,?,'reminder',?)",
            (entity["id"], _B, STAMP),
        )
    credentials = tuple(
        ctx[role]["Authorization"].removeprefix("Bearer ") for role in ("owner", "admin", _A, _B, _GUEST)
    )
    assert all(credentials) and len(set(credentials)) == 5, "inbox_credential_fixture"
    ctx["inbox_secrets"] = (
        *credentials,
        ctx["settings"].api_token,
        ctx["settings"].telegram_bridge_secret,
        "PRIVATE_STYLE_",
        "FOREIGN_PRIVATE",
        PRIVATE,
    )
    ctx["own_bodies"] = tuple(
        ctx["ko_seed"][f"inbox_r10_read_{i}"][field] for i in range(3) for field in ("content", "title")
    )
    yield ctx


def _privacy(ctx, response, *, body_free):
    _equal(all(s not in response.text for s in ctx["inbox_secrets"]), True, "inbox_private_content")
    if body_free:
        assert all(s not in response.text for s in (*ctx["own_bodies"], NOTES)), "inbox_self_bodyfree"
    encoded = json.dumps(_audits(ctx), ensure_ascii=False)
    assert all(s not in encoded for s in (*ctx["inbox_secrets"], *ctx["own_bodies"], NOTES)), (
        "inbox_audit_privacy"
    )


def _audit(ctx, history, started, expected):
    rows = _audits(ctx)
    _equal(rows[: len(history)], history, "inbox_audit_prefix")
    added = rows[len(history) :]
    _equal(len(added), len(expected), "inbox_audit_count")
    _equal(
        [
            (
                r["user_id"],
                r["action"],
                r["target_type"],
                r["target_id"],
                r["before_json"],
                json.loads(r["after_json"]) if r["after_json"] is not None else None,
            )
            for r in added
        ],
        expected,
        "inbox_audit_provenance",
    )
    for row in added:
        assert row["id"] and row["id"] not in {r["id"] for r in history}, "inbox_audit_identity"
        assert started <= datetime.fromisoformat(row["created_at"]) <= datetime.now(UTC), "inbox_audit_clock"


def _public_card(ctx, iid):
    row = ctx["inbox_seed"][iid]
    return {
        "id": iid,
        "raw_object_id": row["raw_object_id"],
        "knowledge_object_id": row["knowledge_object_id"],
        "status": "classified" if iid.endswith("_2") else "pending",
        "suggested_entity_id": None,
        "suggested_action": "review",
        "promotion_score": 0.75,
        "quality_score": 0.5,
        "created_at": STAMP,
        "reviewed_at": None,
        "advisory": {
            "suggestions_present": True,
            "suggestions_bytes": 19,
            "suggested_tags_present": True,
            "suggested_tags_bytes": 7,
            "notes_present": True,
            "notes_chars": len(NOTES),
        },
    }


def _read(ctx, path, role, status=None, limit=2, offset=0, *, explicit=True):
    params = {"limit": limit, "offset": offset}
    if status:
        params["status"] = status
    if path == ADMIN and explicit:
        params["user_id"] = _A
    order = ["inbox_r10_read_2", "inbox_r10_read_1", "inbox_r10_read_0"]
    if status:
        order = [key for key in order if (key.endswith("_2")) == (status == "classified")]
    wanted = order[offset : offset + limit]
    before, history = _state(ctx), _audits(ctx)
    started = datetime.now(UTC).replace(microsecond=0)
    response = ctx["client"].get(path, headers=ctx[role], params=params)
    _equal(response.status_code, 200, "inbox_read_status")
    _privacy(ctx, response, body_free=path == SELF)
    body = response.json()
    _equal([x["id"] for x in body["items"]], wanted, "inbox_page_membership")
    _equal(body["count"], len(wanted), "inbox_page_count")
    if path == SELF:
        _equal(
            body,
            {"items": [_public_card(ctx, key) for key in wanted], "count": len(wanted)},
            "inbox_structural_cards",
        )
    else:
        _equal(
            set(body),
            {"user_id", "items", "count", "total", "limit", "offset"},
            "inbox_admin_envelope_fields",
        )
        _equal(
            {k: body[k] for k in ("user_id", "total", "limit", "offset")},
            {"user_id": _A, "total": len(order), "limit": limit, "offset": offset},
            "inbox_admin_total",
        )
        for item, key in zip(body["items"], wanted, strict=True):
            raw = ctx["raw_seed"][key]
            _equal(
                set(item),
                set(ctx["inbox_seed"][key])
                | {"raw_object", "knowledge_object", "suggested_tags", "suggestions"},
                "inbox_admin_item_fields",
            )
            _equal(
                {k: item[k] for k in ctx["inbox_seed"][key]},
                ctx["inbox_seed"][key],
                "inbox_admin_stored_card",
            )
            _equal(
                item["raw_object"],
                {
                    "id": raw["id"],
                    "source": raw["source"],
                    "source_ref": raw["source_ref"],
                    "content_type": raw["content_type"],
                    "raw_content": raw["raw_content"],
                    "received_at": raw["received_at"],
                    "metadata": json.loads(raw["metadata_json"]),
                },
                "inbox_admin_raw",
            )
            _equal(item["knowledge_object"], ctx["ko_seed"][key], "inbox_admin_knowledge")
            _equal(
                (item["suggested_tags"], item["suggestions"]),
                (["tag"], {"note": "advisory"}),
                "inbox_admin_advisory",
            )
    _equal(_state(ctx), before, "inbox_read_state")
    expected = (
        []
        if path == SELF or role == _A
        else [
            (_ADMIN if role == "admin" else LEGACY_OWNER_USER_ID, "admin.inbox.read", "user", _A, None, None)
        ]
    )
    _audit(ctx, history, started, expected)


def _walk(ctx, path):
    for role in (_A,) if path == SELF else ("owner", "admin", _A):
        _read(ctx, path, role)
        for status in (None, "pending", "classified"):
            for offset in (0, 1, 2, 3):
                _read(ctx, path, role, status, 1, offset)
    if path == ADMIN:
        _read(ctx, path, _A, explicit=False)


def test_self_inbox_exact_structural_cards_pages_and_private_exclusion(inbox_http):
    _walk(inbox_http, SELF)


def test_admin_inbox_exact_target_content_pages_totals_and_audit(inbox_http):
    _walk(inbox_http, ADMIN)


def _refused(ctx, path):
    role = "owner" if path == ADMIN else _A
    params = {"user_id": _A} if path == ADMIN else {}
    cases = [
        (role, {**params, **bad}, status)
        for bad, status in (
            ({"status": "invalid"}, 400),
            ({"limit": 0}, 422),
            ({"limit": 1001}, 422),
            ({"offset": -1}, 422),
            ({"limit": "bad"}, 422),
        )
    ]
    cases += [(None, params, 401), (_B, params, 403), ("revoked", params, 403)]
    for actor, query, status in cases:
        if actor == "revoked":
            capability = "admin.all_data.read" if path == ADMIN else "inbox.read"
            ctx["storage"].set_permission_override(_A, capability, "deny")
            actor = _A
        before, history = _state(ctx), _audits(ctx)
        started = datetime.now(UTC).replace(microsecond=0)
        response = ctx["client"].get(path, headers=ctx[actor] if actor else {}, params=query)
        _equal(response.status_code, status, "inbox_refusal_status")
        _privacy(ctx, response, body_free=True)
        assert "items" not in response.json(), "inbox_refusal_no_items"
        _equal(_state(ctx), before, "inbox_refusal_state")
        expected = []
        if status == 401:
            expected = [
                (
                    "anonymous",
                    "auth.failed",
                    "auth",
                    "invalid_credentials",
                    None,
                    {
                        "method_chars": 3,
                        "path_chars": len(path),
                        "reason": "invalid_credentials",
                        "status_present": True,
                    },
                )
            ]
        elif status == 400 and path == ADMIN:
            # Existing handler audits the requested foreign read before status parsing.
            expected = [(LEGACY_OWNER_USER_ID, "admin.inbox.read", "user", _A, None, None)]
        _audit(ctx, history, started, expected)


@pytest.mark.parametrize("path", [SELF, ADMIN], ids=["self", "admin"])
def test_inbox_refusals_preserve_content_state_and_exact_audit(inbox_http, path):
    _refused(inbox_http, path)


_FAULTS = (
    ("self_count", "inbox_page_count", SELF, False),
    ("self_duplicate", "inbox_page_membership", SELF, False),
    ("admin_total", "inbox_admin_total", ADMIN, False),
    ("admin_raw", "inbox_admin_raw", ADMIN, False),
    ("admin_extra_item", "inbox_admin_item_fields", ADMIN, False),
    ("admin_extra_envelope", "inbox_admin_envelope_fields", ADMIN, False),
    ("private_self", "inbox_private_content", SELF, False),
    ("private_admin", "inbox_private_content", ADMIN, False),
    ("refusal_401", "inbox_self_bodyfree", SELF, True),
    ("refusal_403", "inbox_self_bodyfree", ADMIN, True),
    ("refusal_title", "inbox_self_bodyfree", ADMIN, True),
    ("refusal_credential", "inbox_private_content", ADMIN, True),
    ("refusal_400", "inbox_self_bodyfree", ADMIN, True),
    ("refusal_422", "inbox_self_bodyfree", SELF, True),
    ("read_own", "inbox_read_state", SELF, False),
    ("foreign_write", "inbox_read_state", ADMIN, False),
    ("refusal_write", "inbox_refusal_state", ADMIN, True),
    ("audit_missing", "inbox_audit_count", ADMIN, False),
    ("audit_actor", "inbox_audit_provenance", ADMIN, False),
    ("audit_prefix", "inbox_audit_prefix", ADMIN, True),
)


@pytest.mark.parametrize("fault,code,path,refusal", _FAULTS, ids=[x[0] for x in _FAULTS])
def test_inbox_oracles_detect_actual_http_audit_and_persisted_corruption(
    inbox_http, monkeypatch, fault, code, path, refusal
):
    ctx, storage, hits = inbox_http, inbox_http["storage"], []
    original, log = TestClient.request, storage.log_audit
    if fault in {"audit_missing", "audit_actor"}:

        def changed(entry):
            if entry.action == "admin.inbox.read":
                hits.append(True)
                if fault == "audit_missing":
                    return None
                entry = replace(entry, user_id=_B)
            return log(entry)

        monkeypatch.setattr(storage, "log_audit", changed)

    def altered(self, method, url, **kwargs):
        response = original(self, method, url, **kwargs)
        if hits or method != "GET" or url != path:
            return response
        body = response.json()
        if fault.startswith("refusal_") and fault[8:].isdigit() and response.status_code == int(fault[8:]):
            body["detail"] = ctx["own_bodies"][0]
        elif fault == "refusal_title" and response.status_code == 403:
            body["detail"] = ctx["ko_seed"]["inbox_r10_read_0"]["title"]
            assert body["detail"] == "Архив A1", "inbox_fault_title_fixture"
        elif fault == "refusal_credential" and response.status_code == 403:
            submitted = kwargs.get("headers", {}).get("Authorization")
            if submitted != ctx[_B]["Authorization"]:
                return response
            body["detail"] = submitted.removeprefix("Bearer ")
            assert body["detail"], "inbox_fault_credential_fixture"
        elif response.status_code == 200:
            if fault == "self_count":
                body["count"] += 1
            elif fault == "self_duplicate":
                body["items"][1] = body["items"][0]
            elif fault == "admin_total":
                body["total"] = body["count"]
            elif fault == "admin_raw":
                body["items"][0]["raw_object"]["raw_content"] = "wrong observed body"
            elif fault in {"admin_extra_item", "admin_extra_envelope"}:
                hidden = storage.execute("SELECT id FROM inbox WHERE id='inbox_r10_read_3'").fetchone()[0]
                assert hidden not in [item["id"] for item in body["items"]], "inbox_fault_hidden_fixture"
                target = body["items"][0] if fault == "admin_extra_item" else body
                target["excluded_private_inbox_id"] = hidden
                assert target["excluded_private_inbox_id"] == hidden, "inbox_fault_not_injected"
            elif fault in {"private_self", "private_admin"}:
                body["leak"] = storage.execute(
                    "SELECT content FROM knowledge_objects WHERE title=?", (PRIVATE,)
                ).fetchone()[0]
            elif fault in {"read_own", "foreign_write"}:
                table, field, key = (
                    ("inbox", "classification_notes", "inbox_r10_read_0")
                    if fault == "read_own"
                    else ("knowledge_objects", "title", ctx["foreign"])
                )
                with storage.transaction() as conn:
                    conn.execute(f"UPDATE {table} SET {field}='CORRUPTED_INBOX_READ' WHERE id=?", (key,))
                assert (
                    storage.execute(f"SELECT {field} FROM {table} WHERE id=?", (key,)).fetchone()[0]
                    == "CORRUPTED_INBOX_READ"
                ), "inbox_fault_not_persisted"
            else:
                return response
        elif response.status_code == 403 and fault == "refusal_write":
            with storage.transaction() as conn:
                conn.execute(
                    "UPDATE inbox SET classification_notes='CORRUPTED_INBOX_REFUSAL' WHERE id='inbox_r10_read_0'"
                )
            assert (
                storage.execute(
                    "SELECT classification_notes FROM inbox WHERE id='inbox_r10_read_0'"
                ).fetchone()[0]
                == "CORRUPTED_INBOX_REFUSAL"
            ), "inbox_fault_not_persisted"
        elif response.status_code == 403 and fault == "audit_prefix":
            with storage.transaction() as conn:
                rowid = conn.execute("SELECT MIN(rowid)-1 FROM audit_log").fetchone()[0]
                conn.execute(
                    "INSERT INTO audit_log(rowid,id,user_id,action,target_type,target_id,created_at) VALUES(?,?,?,?,?,?,?)",
                    (rowid, "aud_inbox_prefix", _B, "corrupted", "user", _A, STAMP),
                )
            assert (
                storage.execute("SELECT id FROM audit_log ORDER BY rowid LIMIT 1").fetchone()[0]
                == "aud_inbox_prefix"
            ), "inbox_fault_not_persisted"
        else:
            return response
        hits.append(True)
        return httpx.Response(response.status_code, json=body, request=response.request)

    monkeypatch.setattr(TestClient, "request", altered)
    with pytest.raises(AssertionError, match=code):
        if refusal:
            _refused(ctx, path)
        else:
            _read(ctx, path, "owner" if path == ADMIN else _A)
    assert hits, "inbox_fault_not_injected"
