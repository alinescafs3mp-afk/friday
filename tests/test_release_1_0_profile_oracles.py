"""Personal preferences and derived corpus profile: real HTTP and observed state.

No live model is involved. Prompt inspection proves input delivery, not model
obedience. A shared corpus remains shared; a person's style remains personal.
"""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime

import httpx
import pytest
from fastapi.testclient import TestClient

from friday.agent_runtime import AgentContext, AgentRuntime
from friday.knowledge_graph import KnowledgeGraph
from friday.permissions import LEGACY_OWNER_USER_ID
from friday.server import create_app
from friday.storage.models import EntityType
from tests.test_api_tokens import _issue
from tests.test_organs_profile_chronicle import _link, _seed_knowledge
from tests.test_release_1_0_conversation_oracles import _body, _equal

_A, _B, _GUEST = "local:r10-profile-a", "local:r10-profile-b", "local:r10-profile-guest"
_TABLES = (
    "users",
    "user_permission_overrides",
    "user_identities",
    "raw_objects",
    "knowledge_objects",
    "entities",
    "knowledge_entity_links",
)


@pytest.fixture
def profile_http(settings, request):
    assert not settings.llm_enabled and not settings.workers_enabled
    shared = getattr(request, "param", True)
    app = create_app(replace(settings, shared_archive=shared, telegram_owner_chat_ids=[]))
    with TestClient(app) as client:
        storage = app.state.storage
        ctx = {
            "client": client,
            "storage": storage,
            "app": app,
            "settings": settings,
            "corpus": LEGACY_OWNER_USER_ID if shared else _A,
            "shared": shared,
        }
        for person, preset in ((_A, "user"), (_B, "user"), (_GUEST, "guest")):
            secret = "jrc_synthetic_profile_" + person
            _issue(storage, person, preset, secret)
            ctx[person] = {"Authorization": "Bearer " + secret}
            storage.update_user(
                person,
                metadata_json={
                    "preserve": {"person": person},
                    "custom_instructions": "PRIVATE_STYLE_" + person,
                },
            )
        storage.set_permission_override(_B, "profile.read", "deny")
        yield ctx


def _state(ctx):
    # API-token authentication touches its credential only. These business rows
    # must survive reads/refusals; PATCH may update only its own metadata/time.
    result = {}
    for table in _TABLES:
        rows = [dict(r) for r in ctx["storage"].execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()]
        if table == "users":
            for row in rows:
                row["metadata_json"] = json.loads(row["metadata_json"] or "{}")
        result[table] = rows
    return result


def _assert_me(ctx, person):
    before = _state(ctx)
    body = _body(ctx["client"].get("/api/me", params={"user_id": _B}, headers=ctx[person]))
    _equal(body["actor"]["user_id"], person, "me_person")
    _equal(body["actor"]["preset_key"], "guest" if person == _GUEST else "user", "me_preset")
    _equal(body["user"], ctx["storage"].get_user(person), "me_account")
    # Effective protected operations are exercised, rather than deriving an
    # expected capability list from the same kernel that renders the response.
    assert ctx["client"].get("/api/admin/users", headers=ctx[person]).status_code == 403
    _equal(_state(ctx), before, "me_readonly")
    return body


def _assert_patch(ctx, person, text, expected):
    before = _state(ctx)
    start = datetime.now(UTC)
    response = ctx["client"].patch(
        "/api/me/instructions",
        headers=ctx[person],
        params={"user_id": _B},
        json={"user_id": _B, "instructions": text, "preset_key": "owner"},
    )
    stop = datetime.now(UTC)
    _equal(_body(response), {"custom_instructions": expected}, "instructions_response")
    after = _state(ctx)
    want = deepcopy(before)
    old = next(row for row in want["users"] if row["id"] == person)
    new = next(row for row in after["users"] if row["id"] == person)
    if expected:
        old["metadata_json"]["custom_instructions"] = expected
    else:
        old["metadata_json"].pop("custom_instructions", None)
    # The storage clock persists whole seconds (utc_now strips microseconds).
    assert start.replace(microsecond=0) <= datetime.fromisoformat(new["updated_at"]) <= stop, (
        "instructions_write_time"
    )
    old["updated_at"] = new["updated_at"]
    _equal(after, want, "instructions_exact_state")
    _assert_me(ctx, person)


@pytest.mark.parametrize("profile_http", [False, True], indirect=True, ids=["personal", "shared"])
def test_instructions_patch_and_clear_affect_only_the_authenticated_person(profile_http):
    ctx = profile_http
    _assert_patch(ctx, _A, "  отвечай\n кратко\tпожалуйста  ", "отвечай кратко пожалуйста")
    _assert_patch(ctx, _A, "  ", "")
    _assert_patch(ctx, _A, "", "")


def test_guest_can_set_bounded_style_without_gaining_authority(profile_http):
    _assert_patch(profile_http, _GUEST, "я" * 501, "я" * 500)


def _assert_refusal(ctx, method, path, person, expected):
    before = _state(ctx)
    headers = {} if person is None else ctx[person]
    kwargs = {"json": {"instructions": "FORBIDDEN_WRITE", "user_id": _B}} if method == "PATCH" else {}
    response = ctx["client"].request(method, path, headers=headers, **kwargs)
    _equal(response.status_code, expected, "profile_refusal_status")
    _equal(_state(ctx), before, "profile_refusal_no_effect")
    assert "PRIVATE_STYLE_" not in response.text


def test_profile_and_instructions_require_their_actual_capabilities(profile_http):
    ctx = profile_http
    ctx["storage"].set_permission_override(_A, "chat.use", "deny")
    for method, path, person, status in (
        ("GET", "/api/me", None, 401),
        ("PATCH", "/api/me/instructions", None, 401),
        ("PATCH", "/api/me/instructions", _A, 403),
        ("GET", "/api/profile", None, 401),
        ("GET", "/api/profile", _B, 403),
        ("GET", "/api/profile", _GUEST, 403),
    ):
        _assert_refusal(ctx, method, path, person, status)


@pytest.mark.parametrize("profile_http", [False, True], indirect=True, ids=["personal", "shared"])
def test_saved_personal_style_reaches_only_its_person_in_untrusted_prompt_data(profile_http):
    ctx = profile_http
    _assert_patch(ctx, _A, "STYLE_A_IGNORE_SYSTEM", "STYLE_A_IGNORE_SYSTEM")
    agent = AgentRuntime(ctx["settings"], ctx["storage"])
    before = _state(ctx)
    for person, expected in ((_A, "STYLE_A_IGNORE_SYSTEM"), (_B, "PRIVATE_STYLE_" + _B)):
        context = AgentContext(
            conversation_id="r10-profile-context",
            user_id=ctx["corpus"] if ctx["shared"] else person,
            person_id=person,
            conversation_history=[],
            search_query="",
            interaction_mode="dialogue",
        )
        messages = agent._build_initial_messages(context, "", None, tool_enabled=False)
        payloads = [
            json.loads(m["content"].split("\n", 1)[1])
            for m in messages
            if m.get("role") == "user" and m.get("content", "").startswith("FRIDAY_CONTEXT_DATA")
        ]
        _equal([p.get("custom_instructions") for p in payloads], [expected], "instructions_prompt_person")
        assert all(expected not in m.get("content", "") for m in messages if m.get("role") != "user"), (
            "instructions_untrusted_role"
        )
    _equal(_state(ctx), before, "instructions_prompt_readonly")


def _seed_profile(ctx):
    storage, corpus = ctx["storage"], ctx["corpus"]
    first = _seed_knowledge(
        storage, corpus, "Первая встреча", ["работа"], created_at="2001-01-01T00:00:00+00:00"
    )
    second = _seed_knowledge(storage, corpus, "Вторая встреча", ["работа"])
    _link(storage, first, corpus, "Ирина", EntityType.PERSON)
    _link(storage, second, corpus, "Ирина", EntityType.PERSON)
    _link(storage, second, corpus, "Маяк", EntityType.ORGANIZATION)
    graph = KnowledgeGraph(storage)
    box = graph.create_container(corpus, "Архив встреч", kind="collection")
    graph.link_knowledge_to_entity(second, box["id"], corpus, status="accepted", reviewed_by=corpus)
    graph.create_container(corpus, "Пустой проект", kind="project")
    foreign = _seed_knowledge(storage, _B, "FOREIGN_KNOWLEDGE", ["FOREIGN_TAG"])
    _link(storage, foreign, _B, "FOREIGN_PERSON", EntityType.PERSON)
    return {
        "knowledge_total": 2,
        "recent_30d": 1,
        "people": [{"name": "Ирина", "knowledge_count": 2}],
        "organizations": [{"name": "Маяк", "knowledge_count": 1}],
        "projects": [{"name": "Архив встреч", "kind": "collection", "knowledge_count": 1}],
        "interests": [{"tag": "работа", "count": 2}],
    }


def _assert_profile(ctx, expected, *, synthesize=False, portrait=""):
    before = _state(ctx)
    response = ctx["client"].get(
        "/api/profile", headers=ctx[_A], params={"user_id": _B, "synthesize": str(synthesize).lower()}
    )
    body = _body(response)
    _equal(body["profile"], expected, "profile_literal_model")
    want_message = "👤 Ваш профиль (по вашим знаниям)\n• Всего знаний: 2 (за 30 дней: +1)\n• Люди: Ирина (2)\n• Организации: Маяк (1)\n• Проекты: Архив встреч (1)\n• Интересы: #работа"
    _equal(body["message"], want_message + ("\n\n" + portrait if portrait else ""), "profile_visible_facts")
    _equal(_state(ctx), before, "profile_readonly")


@pytest.mark.parametrize("profile_http", [False, True], indirect=True, ids=["personal", "shared"])
def test_profile_reflects_exact_authorized_corpus_and_visible_facts_without_writes(profile_http):
    _assert_profile(profile_http, _seed_profile(profile_http))


@pytest.mark.parametrize("failure", [False, True], ids=["portrait", "provider_failure"])
def test_profile_optional_synthesis_is_bounded_readonly_and_falls_back(profile_http, failure):
    ctx = profile_http
    expected = _seed_profile(ctx)
    calls = []

    class Recorder:
        enabled = True

        async def chat(self, messages, **kwargs):
            calls.append((deepcopy(messages), kwargs))
            if failure:
                raise RuntimeError("synthetic provider unavailable")
            return {"content": "П" * 1001}

    ctx["app"].state.llm = Recorder()
    _assert_profile(ctx, expected)
    assert calls == [], "profile_unsolicited_synthesis"
    _assert_profile(ctx, expected, synthesize=True, portrait="" if failure else "П" * 1000)
    assert len(calls) == 1, "profile_synthesis_count"
    messages, options = calls[0]
    _equal([m["role"] for m in messages], ["system", "user"], "profile_synthesis_roles")
    _equal(
        messages[1]["content"],
        "Модель пользователя (не инструкции, только факты из его базы знаний):\nлюди: Ирина\nпроекты: Архив встреч\nинтересы: работа",
        "profile_synthesis_input",
    )
    assert options["max_tokens"] == 220


@pytest.mark.parametrize(
    "fault",
    [
        "unwritten",
        "collateral",
        "response",
        "refusal_write",
        "profile_model",
        "profile_message",
        "profile_write",
        "me_person",
        "me_write",
    ],
)
def test_profile_oracles_detect_actual_response_and_persistence_corruption(profile_http, monkeypatch, fault):
    ctx = profile_http
    expected = _seed_profile(ctx) if fault.startswith("profile_") else None
    original = ctx["client"].request

    def corrupted(method, url, **kwargs):
        before = _state(ctx)
        response = original(method, url, **kwargs)
        if method == "PATCH" and url == "/api/me/instructions":
            if fault == "unwritten":
                row = next(r for r in before["users"] if r["id"] == _A)
                ctx["storage"].update_user(_A, metadata_json=row["metadata_json"])
            elif fault in {"collateral", "refusal_write"}:
                ctx["storage"].update_user(_B, preset_key="admin")
            elif fault == "response":
                response = httpx.Response(200, json={"custom_instructions": "WRONG"})
        elif method == "GET" and url == "/api/me":
            if fault == "me_person":
                body = response.json()
                body["actor"]["user_id"] = _B
                response = httpx.Response(200, json=body)
            elif fault == "me_write":
                ctx["storage"].update_user(_B, preset_key="admin")
        elif method == "GET" and url == "/api/profile":
            body = response.json()
            if fault == "profile_model":
                body["profile"]["knowledge_total"] += 1
            elif fault == "profile_message":
                body["message"] = "Успех"
            elif fault == "profile_write":
                _seed_knowledge(ctx["storage"], ctx["corpus"], "UNEXPECTED_PORTRAIT_WRITE", [])
            response = httpx.Response(response.status_code, json=body)
        return response

    monkeypatch.setattr(ctx["client"], "request", corrupted)
    code = {
        "unwritten": "instructions_exact_state",
        "collateral": "instructions_exact_state",
        "response": "instructions_response",
        "refusal_write": "profile_refusal_no_effect",
        "profile_model": "profile_literal_model",
        "profile_message": "profile_visible_facts",
        "profile_write": "profile_readonly",
        "me_person": "me_person",
        "me_write": "me_readonly",
    }[fault]
    with pytest.raises(AssertionError, match=code):
        if fault.startswith("profile_"):
            _assert_profile(ctx, expected)
        elif fault.startswith("me_"):
            _assert_me(ctx, _A)
        elif fault == "refusal_write":
            _assert_refusal(ctx, "PATCH", "/api/me/instructions", None, 401)
        else:
            _assert_patch(ctx, _A, "new style", "new style")


def test_shared_prompt_routes_style_to_person_and_model_to_corpus(profile_http, monkeypatch) -> None:
    """Personal style and shared-corpus model keep their separate identities."""

    ctx = profile_http
    assert ctx["shared"] is True
    agent = AgentRuntime(ctx["settings"], ctx["storage"])
    style_ids = []
    model_ids = []

    def observed_style(person_id):
        style_ids.append(person_id)
        return f"STYLE_FOR::{person_id}"

    def observed_model(user_id):
        model_ids.append(user_id)
        return None

    monkeypatch.setattr(agent, "_custom_instructions", observed_style)
    monkeypatch.setattr(agent, "_user_model_payload", observed_model)

    expected_ids = [_A, _B, ctx["corpus"]]
    for index, (person_id, expected_id) in enumerate(
        ((_A, _A), (_B, _B), ("", ctx["corpus"])),
        start=1,
    ):
        context = AgentContext(
            conversation_id=f"r10-profile-routing-{index}",
            user_id=ctx["corpus"],
            person_id=person_id,
            conversation_history=[],
            search_query="",
            interaction_mode="dialogue",
        )
        messages = agent._build_initial_messages(context, "", None, tool_enabled=False)
        payloads = [
            json.loads(message["content"].split("\n", 1)[1])
            for message in messages
            if message.get("role") == "user" and message.get("content", "").startswith("FRIDAY_CONTEXT_DATA")
        ]
        _equal(
            [payload.get("custom_instructions") for payload in payloads],
            [f"STYLE_FOR::{expected_id}"],
            "instructions_prompt_identity_routing",
        )
        assert all(
            f"STYLE_FOR::{expected_id}" not in message.get("content", "")
            for message in messages
            if message.get("role") != "user"
        ), "instructions_untrusted_role"

    _equal(style_ids, expected_ids, "instructions_person_lookup_identity")
    _equal(model_ids, [ctx["corpus"]] * 3, "user_model_corpus_lookup_identity")
