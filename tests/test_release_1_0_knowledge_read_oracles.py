"""Admin knowledge reads: exact content, selection, provenance and no writes."""

from __future__ import annotations

import json

import httpx
import pytest

from friday.knowledge_graph import KnowledgeGraph
from friday.storage.models import EntityType
from tests.test_api_tokens import _issue
from tests.test_organs_profile_chronicle import _seed_knowledge
from tests.test_release_1_0_conversation_oracles import _body, _equal
from tests.test_release_1_0_profile_oracles import _A, _B, _state
from tests.test_release_1_0_profile_oracles import profile_http as profile_http

_ADMIN = "local:r10-knowledge-admin"
_KINDS = ("list", "tags", "timeline", "inspect", "diff", "mentions", "source", "containers")


@pytest.fixture
def knowledge_read_http(profile_http):
    ctx = profile_http
    storage = ctx["storage"]
    token = "jrc_synthetic_r10_knowledge_admin"
    _issue(storage, _ADMIN, "admin", token)
    ctx["admin"] = {"Authorization": "Bearer " + token}
    ctx["first"] = _seed_knowledge(storage, _A, "Иванов и документ A1", ["alpha"])
    ctx["second"] = _seed_knowledge(storage, _A, "Документ A2", ["alpha", "beta"])
    ctx["undated"] = _seed_knowledge(storage, _A, "Без даты", [])
    ctx["foreign"] = _seed_knowledge(storage, _B, "FOREIGN_PRIVATE_документ", ["foreign"])
    storage.update_knowledge_fields(
        ctx["first"], _A, title="Архив A1", metadata_json={"document_date": "2024-03-17"}
    )
    storage.update_knowledge_fields(ctx["second"], _A, metadata_json={"document_date": "2024-04-08"})
    graph = KnowledgeGraph(storage)
    person = graph.create_entity(_A, "Иванов", EntityType.PERSON)
    graph.link_knowledge_to_entity(ctx["first"], person["id"], _A, status="accepted", reviewed_by=_A)
    ctx["person"] = person["id"]
    box = graph.create_container(_A, "Материалы", kind="collection")
    graph.link_knowledge_to_entity(ctx["first"], box["id"], _A, status="accepted", reviewed_by=_A)
    ctx["box"] = box["id"]
    graph.create_container(_B, "FOREIGN_CONTAINER", kind="collection")
    yield ctx


def _read_state(ctx):
    result = _state(ctx)
    for table in (
        "knowledge_object_versions",
        "entity_versions",
        "inbox",
        "relations",
        "relation_revision_context",
        "relation_revisions",
        "relation_candidates",
    ):
        result[table] = [
            dict(r) for r in ctx["storage"].execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
        ]
    for row in result["relation_revision_context"]:
        # Durable logical clock advances on ordinary transactions (including
        # credential/audit writes). Relation contents/history stay exact; batch
        # and recorded_at must still be cleared at the transaction boundary.
        row.pop("observed_at")
    return result


def _request_spec(ctx, kind):
    paths = {
        "list": "/knowledge",
        "tags": "/knowledge/tags",
        "timeline": "/knowledge/timeline",
        "inspect": f"/knowledge/{ctx['first']}",
        "diff": f"/knowledge/{ctx['first']}/diff",
        "mentions": f"/knowledge/{ctx['first']}/entity-mentions",
        "source": "/source-search",
        "containers": "/containers",
    }
    params = {"user_id": _A}
    if kind == "list":
        params.update(q="Архив", limit=1, offset=0)
    elif kind == "timeline":
        params.update(since="2024-03-01", until="2024-03-31", limit=10)
    elif kind == "source":
        params.update(q="Иванов", limit=10)
    return "/api/admin" + paths[kind], params


def _audits(ctx):
    return [dict(r) for r in ctx["storage"].execute("SELECT * FROM audit_log ORDER BY rowid").fetchall()]


def _assert_read(ctx, kind):
    path, params = _request_spec(ctx, kind)
    before, audit_before = _read_state(ctx), _audits(ctx)
    response = ctx["client"].get(path, headers=ctx["admin"], params=params)
    body = _body(response)
    assert "FOREIGN_PRIVATE" not in response.text and "FOREIGN_CONTAINER" not in response.text, (
        "knowledge_foreign_content"
    )
    if kind == "list":
        _equal(
            {k: body[k] for k in ("user_id", "count", "total", "limit", "offset")},
            {"user_id": _A, "count": 1, "total": 1, "limit": 1, "offset": 0},
            "knowledge_filtered_page",
        )
        _equal(
            [(r["id"], r["title"]) for r in body["items"]],
            [(ctx["first"], "Архив A1")],
            "knowledge_list_members",
        )
    elif kind == "tags":
        _equal(
            body,
            {"user_id": _A, "items": [{"tag": "alpha", "count": 2}, {"tag": "beta", "count": 1}], "count": 2},
            "knowledge_tags",
        )
    elif kind == "timeline":
        _equal(
            {k: body[k] for k in ("user_id", "granularity", "since", "until", "buckets", "undated", "limit")},
            {
                "user_id": _A,
                "granularity": "day",
                "since": "2024-03-01",
                "until": "2024-03-31",
                "buckets": [{"bucket": "2024-03-17", "count": 1}],
                "undated": 1,
                "limit": 10,
            },
            "knowledge_timeline",
        )
        _equal(
            [(r["id"], r["title"]) for r in body["items"]],
            [(ctx["first"], "Архив A1")],
            "knowledge_timeline_members",
        )
    elif kind == "inspect":
        _equal(
            (
                body["item"]["id"],
                body["item"]["title"],
                body["item"]["content"],
                body["item"]["tags"],
                body["item"]["metadata"],
            ),
            (ctx["first"], "Архив A1", "Иванов и документ A1", ["alpha"], {"document_date": "2024-03-17"}),
            "knowledge_inspect_item",
        )
        _equal(body["raw_object"]["raw_content"], "Иванов и документ A1", "knowledge_inspect_source")
        _equal(body["raw_object"]["id"], body["item"]["raw_object_id"], "knowledge_inspect_source_id")
        _equal(
            sorted((r["version"], json.loads(r["snapshot_json"])["title"]) for r in body["versions"]),
            [(1, "Иванов и документ A1"), (2, "Архив A1")],
            "knowledge_inspect_history",
        )
        _equal(
            {(r["entity_id"], r["status"]) for r in body["entity_links"]},
            {(ctx["person"], "accepted"), (ctx["box"], "accepted")},
            "knowledge_inspect_links",
        )
        _equal(body["inbox"], None, "knowledge_inspect_inbox")
    elif kind == "diff":
        _equal(
            (body["from_version"], body["to_version"], body["available_versions"]),
            (1, 2, [1, 2]),
            "knowledge_diff_versions",
        )
        _equal(
            body["changes"],
            {
                "title": {"kind": "scalar", "from": "Иванов и документ A1", "to": "Архив A1"},
                "metadata": {
                    "kind": "map",
                    "added": {"document_date": "2024-03-17"},
                    "removed": {},
                    "changed": {},
                },
            },
            "knowledge_diff_fields",
        )
    elif kind == "mentions":
        _equal(
            body,
            {
                "knowledge_object_id": ctx["first"],
                "items": [{"start": 0, "end": 6, "name": "Иванов", "entity_id": ctx["person"]}],
                "count": 1,
                "truncated": False,
            },
            "knowledge_mentions",
        )
    elif kind == "source":
        _equal(
            {k: body[k] for k in ("user_id", "query", "count", "limit")},
            {"user_id": _A, "query": "Иванов", "count": 1, "limit": 10},
            "knowledge_source_count",
        )
        _equal(
            [(r["knowledge_object_id"], r["excerpt"]) for r in body["items"]],
            [(ctx["first"], "Иванов и документ A1")],
            "knowledge_source_excerpt",
        )
        assert "total" not in body, "knowledge_source_page_is_not_total"
    elif kind == "containers":
        _equal(
            (body["user_id"], body["count"], body["matched_at_least"], body["truncated"]),
            (_A, 1, 1, False),
            "knowledge_container_count",
        )
        _equal(
            [(r["id"], r["name"], r["knowledge_count"]) for r in body["items"]],
            [(ctx["box"], "Материалы", 1)],
            "knowledge_container_members",
        )
    _equal(_read_state(ctx), before, "knowledge_read_preserves_state")
    rows = _audits(ctx)
    _equal(rows[: len(audit_before)], audit_before, "knowledge_read_preserves_audit_history")
    delta = rows[len(audit_before) :]
    action = {
        "inspect": "admin.knowledge.inspect",
        "diff": "admin.knowledge.diff",
        "source": "admin.source.search",
        "containers": "admin.containers.read",
    }.get(kind, "admin.knowledge.read")
    _equal(
        [(r["user_id"], r["action"], r["target_type"], r["target_id"]) for r in delta],
        [(_ADMIN, action, "user", _A)],
        "knowledge_read_attributed_audit",
    )
    audit_text = json.dumps(delta, ensure_ascii=False)
    assert not any(
        marker in audit_text
        for marker in ("Иванов", "Архив A1", "Документ A2", "Без даты", "FOREIGN_PRIVATE", "Материалы")
    ), "knowledge_read_audit_no_body"


@pytest.mark.parametrize("kind", _KINDS)
def test_knowledge_reads_return_the_requested_corpus_facts_and_attributed_audit(knowledge_read_http, kind):
    _assert_read(knowledge_read_http, kind)


def _assert_refusal(ctx, kind, headers, status):
    before, audit_before = _read_state(ctx), _audits(ctx)
    path, params = _request_spec(ctx, kind)
    response = ctx["client"].get(path, headers=headers, params=params)
    _equal(response.status_code, status, "knowledge_read_refusal_status")
    _equal(_read_state(ctx), before, "knowledge_read_refusal_no_effect")
    # Denials may have their own security audit; they must not pretend to have
    # performed a successful admin knowledge/source/container read.
    assert not any(
        r["action"].startswith(("admin.knowledge.", "admin.source.", "admin.containers."))
        for r in _audits(ctx)[len(audit_before) :]
    ), "knowledge_refusal_success_audit"
    assert "Иванов" not in response.text and "FOREIGN_PRIVATE" not in response.text


@pytest.mark.parametrize("kind", _KINDS)
def test_knowledge_read_surfaces_enforce_anonymous_ordinary_and_explicit_denial(knowledge_read_http, kind):
    ctx = knowledge_read_http
    ctx["storage"].set_permission_override(_ADMIN, "admin.all_data.read", "deny")
    for headers, status in (({}, 401), (ctx[_A], 403), (ctx["admin"], 403)):
        _assert_refusal(ctx, kind, headers, status)


@pytest.mark.parametrize(
    "fault",
    [
        "filtered_total",
        "missing_history",
        "wrong_source",
        "wrong_diff",
        "missing_audit",
        "read_write",
        "refusal_write",
    ],
)
def test_knowledge_read_oracles_catch_corrupted_real_responses_audit_and_writes(
    knowledge_read_http, monkeypatch, fault
):
    ctx = knowledge_read_http
    kind = {"missing_history": "inspect", "wrong_source": "source", "wrong_diff": "diff"}.get(fault, "list")
    path, _ = _request_spec(ctx, kind)
    original = ctx["client"].request
    if fault == "missing_audit":
        monkeypatch.setattr(ctx["storage"], "log_audit", lambda *args, **kwargs: None)

    def corrupted(method, url, **kwargs):
        response = original(method, url, **kwargs)
        if method == "GET" and url == path:
            if fault in {"read_write", "refusal_write"}:
                ctx["storage"].update_knowledge_fields(ctx["foreign"], _B, title="UNEXPECTED_WRITE")
            elif fault != "missing_audit":
                body = response.json()
                if fault == "filtered_total":
                    body["total"] = 3
                elif fault == "missing_history":
                    body["versions"].pop()
                elif fault == "wrong_source":
                    body["items"][0]["excerpt"] = "WRONG_SOURCE"
                elif fault == "wrong_diff":
                    body["changes"]["title"]["to"] = "WRONG_DIFF"
                response = httpx.Response(response.status_code, json=body)
        return response

    monkeypatch.setattr(ctx["client"], "request", corrupted)
    code = {
        "filtered_total": "knowledge_filtered_page",
        "missing_history": "knowledge_inspect_history",
        "wrong_source": "knowledge_source_excerpt",
        "wrong_diff": "knowledge_diff_fields",
        "missing_audit": "knowledge_read_attributed_audit",
        "read_write": "knowledge_read_preserves_state",
        "refusal_write": "knowledge_read_refusal_no_effect",
    }[fault]
    with pytest.raises(AssertionError, match=code):
        if fault == "refusal_write":
            _assert_refusal(ctx, kind, {}, 401)
        else:
            _assert_read(ctx, kind)


@pytest.mark.parametrize("refused", [False, True], ids=["read", "refusal"])
def test_knowledge_read_preservation_catches_an_actual_relation_written_after_http(
    knowledge_read_http, monkeypatch, refused
):
    ctx = knowledge_read_http
    original = ctx["client"].request
    injected = False

    def damaged_request(method, url, **kwargs):
        nonlocal injected
        response = original(method, url, **kwargs)
        assert method == "GET" and url == "/api/admin/knowledge"
        KnowledgeGraph(ctx["storage"]).create_relation(_A, ctx["person"], ctx["box"])
        injected = True
        return response

    monkeypatch.setattr(ctx["client"], "request", damaged_request)
    with pytest.raises(
        AssertionError,
        match="knowledge_read_refusal_no_effect" if refused else "knowledge_read_preserves_state",
    ):
        if refused:
            _assert_refusal(ctx, "list", ctx[_A], 403)
        else:
            _assert_read(ctx, "list")
    assert injected, "knowledge_relation_fault_exercised"
