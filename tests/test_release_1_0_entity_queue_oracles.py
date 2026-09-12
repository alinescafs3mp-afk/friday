"""Entity review reads: exact queue windows, suggestions, groups and no writes."""

from __future__ import annotations

import json
from dataclasses import replace

import httpx
import pytest

from friday.knowledge_graph import KnowledgeGraph
from friday.storage.models import EntityType
from tests.test_organs_profile_chronicle import _seed_knowledge
from tests.test_release_1_0_conversation_oracles import _body, _equal
from tests.test_release_1_0_knowledge_mutation_oracles import _state
from tests.test_release_1_0_knowledge_read_oracles import _A, _ADMIN, _B, _audits
from tests.test_release_1_0_knowledge_read_oracles import knowledge_read_http as knowledge_read_http
from tests.test_release_1_0_profile_oracles import profile_http as profile_http

_KINDS = ("queue", "groups", "suggestions")


@pytest.fixture
def entity_queue_http(knowledge_read_http):
    ctx = knowledge_read_http
    storage = ctx["storage"]
    graph = KnowledgeGraph(storage)
    for name in ("amberquartz", "zephyrquartz"):
        ctx[name] = graph.create_entity(_A, name, EntityType.CONCEPT)["id"]
    for key, text, count in (
        ("first", "amberquartz; zephyrquartz;", 5),
        ("second", "amberquartz;", 1),
        ("undated", "zephyrquartz;", 1),
        ("foreign", "FOREIGN_PRIVATE_очередь", 99),
    ):
        storage.update_knowledge_fields(
            ctx[key],
            _B if key == "foreign" else _A,
            content=text,
            metadata_json={"entity_suggestion_count": count},
        )
    graph.link_knowledge_to_entity(
        ctx["undated"],
        ctx["zephyrquartz"],
        _A,
        status="rejected",
        reviewed_by=_A,
    )
    ctx["deleted"] = _seed_knowledge(storage, _A, "DELETED_PRIVATE_очередь", [])
    storage.update_knowledge_fields(ctx["deleted"], _A, metadata_json={"entity_suggestion_count": 88})
    assert storage.soft_delete_knowledge_object(ctx["deleted"], _A)
    yield ctx


def _spec(ctx, kind, empty=False):
    params = {"user_id": _A}
    if kind == "queue":
        params.update(limit=1, offset=2 if empty else 0)
        return "/api/admin/entity-suggestions/queue", params
    if kind == "groups":
        params.update(scan=2, min_docs=3 if empty else 2)
        return "/api/admin/entity-suggestions/groups", params
    key = ctx["undated"] if empty else ctx["first"]
    return f"/api/admin/knowledge/{key}/entity-suggestions", params


def _audit(ctx, before, target=_A, anonymous_path=None, denied=False):
    rows = _audits(ctx)
    _equal(rows[: len(before)], before, "entity_queue_audit_history")
    delta = rows[len(before) :]
    expected = [] if denied else [(_ADMIN, "admin.entity_suggestions.read", "user", target)]
    if anonymous_path:
        expected = [("anonymous", "auth.failed", "auth", "invalid_credentials")]
    _equal(
        [(r["user_id"], r["action"], r["target_type"], r["target_id"]) for r in delta],
        expected,
        "entity_queue_audit_actor",
    )
    if anonymous_path:
        _equal(
            json.loads(delta[0]["after_json"]),
            {
                "reason": "invalid_credentials",
                "status_present": True,
                "method_chars": 3,
                "path_chars": len(anonymous_path),
            },
            "entity_queue_security_audit",
        )
    elif not denied:
        _equal((delta[0]["before_json"], delta[0]["after_json"]), (None, None), "entity_queue_audit_payload")
    encoded = json.dumps(delta, ensure_ascii=False)
    assert not any(s in encoded for s in ("amberquartz", "zephyrquartz", "Архив A1", "FOREIGN_PRIVATE")), (
        "entity_queue_audit_privacy"
    )


def _suggestion(ctx, name):
    return {
        "name": name,
        "entity_type": "concept",
        "confidence": 0.97,
        "method": "existing_entity_exact_mention",
        "entity_id": ctx[name],
        "matched_as": name,
    }


def _assert_read(ctx, kind, *, empty=False, decided=False):
    before, audits = _state(ctx), _audits(ctx)
    path, params = _spec(ctx, kind, empty)
    response = ctx["client"].get(path, headers=ctx["admin"], params=params)
    body = _body(response)
    _equal(_state(ctx), before, "entity_queue_read_no_effect")
    _audit(ctx, audits)
    if kind == "queue":
        original = next(r for r in before["knowledge_objects"] if r["id"] == ctx["first"])
        items = (
            []
            if empty
            else [
                {
                    "id": ctx["first"],
                    "title": "Архив A1",
                    "updated_at": original["updated_at"],
                    "pending": 2 if decided else 3,
                }
            ]
        )
        expected = {
            "user_id": _A,
            "items": items,
            "count": len(items),
            "total": 2,
            "estimate": True,
            "limit": 1,
            "offset": 2 if empty else 0,
        }
    elif kind == "groups":
        groups = (
            []
            if empty or decided
            else [
                {
                    "name": "amberquartz",
                    "entity_type": "concept",
                    "method": "existing_entity_exact_mention",
                    "confidence_max": 0.97,
                    "documents": [
                        {"id": ctx["first"], "title": "Архив A1"},
                        {"id": ctx["second"], "title": "Документ A2"},
                    ],
                    "document_count": 2,
                }
            ]
        )
        expected = {
            "user_id": _A,
            "groups": groups,
            "count": len(groups),
            "scanned_documents": 2,
            "estimate": True,
        }
    else:
        names = [] if empty else (["zephyrquartz"] if decided else ["amberquartz", "zephyrquartz"])
        expected = {
            "knowledge_object_id": ctx["undated"] if empty else ctx["first"],
            "items": [_suggestion(ctx, name) for name in names],
            "count": len(names),
            "decided": 1 if empty else (3 if decided else 2),
        }
    _equal(body, expected, "entity_queue_" + kind + "_literal")


@pytest.mark.parametrize("kind", _KINDS)
def test_entity_review_reads_observe_exact_membership_window_and_audit(entity_queue_http, kind):
    _assert_read(entity_queue_http, kind)


@pytest.mark.parametrize("kind", _KINDS)
def test_entity_review_empty_windows_are_honest_without_writing(entity_queue_http, kind):
    _assert_read(entity_queue_http, kind, empty=True)


@pytest.mark.parametrize("kind", ("suggestions", "groups"))
def test_entity_review_sentence_final_period_keeps_a_literal_existing_name(entity_queue_http, kind):
    # The documented phrase scanner strips sentence-final punctuation while
    # preserving dotted identifiers. A period must not erase an exact mention.
    # Keep this observed source failure separate from the sound control fixture.
    ctx = entity_queue_http
    key, text = (
        ("first", "amberquartz; zephyrquartz.") if kind == "suggestions" else ("second", "amberquartz.")
    )
    ctx["storage"].update_knowledge_fields(ctx[key], _A, content=text)
    _assert_read(ctx, kind)


@pytest.mark.parametrize("status", ("accepted", "rejected", "suggested"))
def test_entity_review_existing_decisions_are_not_offered_again(entity_queue_http, status):
    ctx = entity_queue_http
    KnowledgeGraph(ctx["storage"]).link_knowledge_to_entity(
        ctx["first"],
        ctx["amberquartz"],
        _A,
        status=status,
        reviewed_by=_A,
    )
    for kind in _KINDS:
        _assert_read(ctx, kind, decided=True)


def _assert_denied(ctx, kind, headers, expected_status):
    before, audits = _state(ctx), _audits(ctx)
    path, params = _spec(ctx, kind)
    response = ctx["client"].get(path, headers=headers, params=params)
    _equal(response.status_code, expected_status, "entity_queue_refusal_status")
    _equal(_state(ctx), before, "entity_queue_refusal_no_effect")
    _audit(ctx, audits, anonymous_path=path if expected_status == 401 else None, denied=True)
    assert "amberquartz" not in response.text and "FOREIGN_PRIVATE" not in response.text, (
        "entity_queue_refusal_privacy"
    )


@pytest.mark.parametrize("kind", _KINDS)
def test_entity_review_requires_authority_and_respects_explicit_denial(entity_queue_http, kind):
    ctx = entity_queue_http
    for headers, status in (({}, 401), (ctx[_A], 403)):
        _assert_denied(ctx, kind, headers, status)
    ctx["storage"].set_permission_override(_ADMIN, "admin.all_data.read", "deny")
    _assert_denied(ctx, kind, ctx["admin"], 403)


@pytest.mark.parametrize("kind", _KINDS)
def test_entity_review_invalid_windows_are_refused_without_any_effect(entity_queue_http, kind):
    ctx = entity_queue_http
    path, params = _spec(ctx, kind)
    if kind == "suggestions":
        params.pop("user_id")
    else:
        params["limit" if kind == "queue" else "scan"] = 0
    before, audits = _state(ctx), _audits(ctx)
    response = ctx["client"].get(path, headers=ctx["admin"], params=params)
    _equal(response.status_code, 422, "entity_queue_invalid_status")
    _equal((_state(ctx), _audits(ctx)), (before, audits), "entity_queue_invalid_no_effect")


@pytest.mark.parametrize("target,key", [(_B, "first"), (_A, "deleted"), (_A, "missing")])
def test_entity_document_suggestions_refuse_wrong_deleted_and_missing_targets(entity_queue_http, target, key):
    ctx = entity_queue_http
    before, audits = _state(ctx), _audits(ctx)
    response = ctx["client"].get(
        f"/api/admin/knowledge/{ctx.get(key, 'ko_missing_039')}/entity-suggestions",
        headers=ctx["admin"],
        params={"user_id": target},
    )
    _equal(response.status_code, 404, "entity_queue_target_status")
    _equal(_state(ctx), before, "entity_queue_target_no_effect")
    _audit(ctx, audits, target=target)


_FAULTS = (
    ("queue_total", "queue", "entity_queue_queue_literal"),
    ("queue_foreign", "queue", "entity_queue_queue_literal"),
    ("queue_estimate", "queue", "entity_queue_queue_literal"),
    ("group_member", "groups", "entity_queue_groups_literal"),
    ("group_window", "groups", "entity_queue_groups_literal"),
    ("suggestion_order", "suggestions", "entity_queue_suggestions_literal"),
    ("suggestion_foreign", "suggestions", "entity_queue_suggestions_literal"),
    ("read_write", "groups", "entity_queue_read_no_effect"),
    ("refusal_write", "queue", "entity_queue_refusal_no_effect"),
    ("missing_audit", "suggestions", "entity_queue_audit_actor"),
    ("audit_actor", "queue", "entity_queue_audit_actor"),
)


@pytest.mark.parametrize("fault,kind,code", _FAULTS, ids=[f[0] for f in _FAULTS])
def test_entity_review_oracles_catch_actual_response_audit_and_persistence_faults(
    entity_queue_http,
    monkeypatch,
    fault,
    kind,
    code,
):
    ctx = entity_queue_http
    original = ctx["client"].request
    injected = False
    if fault in {"missing_audit", "audit_actor"}:
        writer = ctx["storage"].log_audit

        def damaged_audit(record):
            nonlocal injected
            injected = True
            if fault == "audit_actor":
                return writer(replace(record, user_id=_B))
            return None

        monkeypatch.setattr(ctx["storage"], "log_audit", damaged_audit)

    def damaged_request(method, url, **kwargs):
        nonlocal injected
        response = original(method, url, **kwargs)
        if fault in {"read_write", "refusal_write"}:
            KnowledgeGraph(ctx["storage"]).create_relation(_A, ctx["person"], ctx["box"])
        elif fault not in {"missing_audit", "audit_actor"}:
            body = response.json()
            if fault == "queue_total":
                body["total"] = 1
            elif fault == "queue_foreign":
                body["items"][0]["id"] = ctx["foreign"]
            elif fault == "queue_estimate":
                body["estimate"] = False
            elif fault == "group_member":
                body["groups"][0]["documents"][1]["id"] = ctx["foreign"]
            elif fault == "group_window":
                body["scanned_documents"] = 99
            elif fault == "suggestion_order":
                body["items"].reverse()
            elif fault == "suggestion_foreign":
                body["items"][0]["entity_id"] = ctx["box"]
            response = httpx.Response(response.status_code, json=body)
        if fault not in {"missing_audit", "audit_actor"}:
            injected = True
        return response

    monkeypatch.setattr(ctx["client"], "request", damaged_request)
    with pytest.raises(AssertionError, match=code):
        if fault == "refusal_write":
            _assert_denied(ctx, kind, ctx[_A], 403)
        else:
            _assert_read(ctx, kind)
    assert injected, "entity_queue_fault_exercised"
