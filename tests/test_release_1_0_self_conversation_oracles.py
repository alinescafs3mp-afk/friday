"""Self-service conversation HTTP under shared archive; no chat/model/sender."""

from __future__ import annotations

import copy
import json
from dataclasses import replace

import httpx
import pytest
from fastapi.testclient import TestClient

from friday.permissions import LEGACY_OWNER_USER_ID
from friday.storage.models import KnowledgeObject, RawObject, new_id
from tests.test_api_vertical_slice import _bridge_get, _bridge_json
from tests.test_release_1_0_conversation_oracles import (
    _PEOPLE,
    _PRIVATE,
    _assert_messages,
    _body,
    _equal,
)
from tests.test_release_1_0_conversation_oracles import (
    conversation_http as conversation_http,
)


@pytest.fixture(name="settings")
def shared_settings(settings):
    return replace(settings, shared_archive=True, telegram_owner_chat_ids=[])


@pytest.fixture
def self_http(conversation_http, settings):
    ctx = conversation_http
    ctx["settings"] = settings
    person, foreign = _PEOPLE[:2]
    token = _body(ctx["client"].post("/api/admin/tokens", headers=ctx["owner"], json={"user_id": foreign}))[
        "token"
    ]
    ctx["foreign_headers"] = {"Authorization": f"Bearer {token}"}
    for who in (person, foreign):
        ctx["storage"].set_channel_conversation(
            who, "api", "same-channel", ctx["conversations"][who][0]["id"]
        )
    return ctx


def _assert_listing(ctx):
    client, storage, person = ctx["client"], ctx["storage"], _PEOPLE[0]
    convs = ctx["conversations"][person]
    storage.set_conversation_archived(convs[2]["id"], person, True)
    for include, expected in [(False, [convs[1], convs[0]]), (True, [convs[2], convs[1], convs[0]])]:
        body = _body(
            client.get(
                "/api/conversations",
                params={"include_archived": str(include).lower()},
                headers=ctx["ordinary"],
            )
        )
        _equal(set(body), {"items", "count"}, "self_list_shape")
        _equal(body["count"], len(expected), "self_list_count")
        _equal([row["id"] for row in body["items"]], [row["id"] for row in expected], "self_list_members")
        assert all(row["user_id"] == person for row in body["items"]), "self_list_person"
    foreign = _body(client.get("/api/conversations", headers=ctx["foreign_headers"]))
    _equal(
        set(row["id"] for row in foreign["items"]),
        {row["id"] for row in ctx["conversations"][_PEOPLE[1]]},
        "self_list_foreign_account",
    )


def test_self_listing_keeps_personal_membership_and_archive_visibility_in_shared_archive(self_http):
    _assert_listing(self_http)


def _assert_history(ctx):
    person, client = _PEOPLE[0], ctx["client"]
    cid = ctx["conversations"][person][-1]["id"]
    for limit, expected in [(1, ctx["messages"][person][-1:]), (10, ctx["messages"][person][-2:])]:
        body = _body(
            client.get(f"/api/conversations/{cid}/messages", params={"limit": limit}, headers=ctx["ordinary"])
        )
        _equal(set(body), {"items", "count"}, "self_history_shape")
        _equal(body["count"], len(expected), "self_history_count")
        _assert_messages(body, expected)
    for headers in (ctx["foreign_headers"], ctx["owner"]):
        assert client.get(f"/api/conversations/{cid}/messages", headers=headers).status_code == 404
    assert (
        client.get(
            f"/api/conversations/{cid}/messages", params={"limit": 0}, headers=ctx["ordinary"]
        ).status_code
        == 422
    )


def test_self_history_has_exact_tail_and_foreign_owner_cannot_read_it(self_http):
    _assert_history(self_http)


def _export_text(conv, rows, *, truncated=False, limit=500):
    lines = [
        "# Friday conversation export",
        f"# conversation_id: {conv['id']}",
        f"# title: {conv['title']}",
        f"# messages: {len(rows)}",
    ]
    if truncated:
        lines.append(
            f"# note: показаны последние {limit} сообщений (потолок выгрузки; более ранние не включены)"
        )
    lines.append("")
    for row in rows:
        text = row["content"].replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\\n")
        lines.append(f"[{row['created_at']}] {row['role']}: {text}")
    return "\n".join([*lines, ""])


def _assert_export(ctx):
    person, storage, client = _PEOPLE[0], ctx["storage"], ctx["client"]
    conv = ctx["conversations"][person][0]
    last = storage.store_message(conv["id"], person, "user", "first line\nsecond line\rthird line")
    rows = [*ctx["messages"][person][:2], last]
    for params, expected in [
        ({}, _export_text(conv, rows)),
        ({"limit": 1}, _export_text(conv, [last], truncated=True, limit=1)),
    ]:
        response = client.get(
            f"/api/conversations/{conv['id']}/export", params=params, headers=ctx["ordinary"]
        )
        _equal(response.status_code, 200, "self_export_status")
        assert response.headers["content-type"].startswith("text/plain; charset=utf-8"), "self_export_type"
        _equal(
            response.headers["content-disposition"],
            f'attachment; filename="jericho-{conv["id"][:24]}.txt"',
            "self_export_attachment",
        )
        _equal(response.content, expected.encode(), "self_export_bytes")
        assert _PEOPLE[1] not in response.text and _PRIVATE not in response.text, (
            "self_export_foreign_metadata"
        )
    for headers in (ctx["foreign_headers"], ctx["owner"]):
        assert client.get(f"/api/conversations/{conv['id']}/export", headers=headers).status_code == 404
    assert (
        client.get(
            f"/api/conversations/{conv['id']}/export", params={"limit": 501}, headers=ctx["ordinary"]
        ).status_code
        == 422
    )


def test_self_export_returns_exact_download_bytes_order_and_truncation_window(self_http):
    _assert_export(self_http)


def _assert_search(ctx):
    storage, client, person = ctx["storage"], ctx["client"], _PEOPLE[0]
    c1, c2 = (row["id"] for row in ctx["conversations"][person][:2])
    marker = "rtenuniquefrozenlocator"
    found = [storage.store_message(cid, person, "user", marker) for cid in (c1, c1, c2)]
    foreign_id = ctx["conversations"][_PEOPLE[1]][0]["id"]
    storage.store_message(foreign_id, _PEOPLE[1], "user", marker)
    for params, expected in [
        ({"limit": 1}, found[-1:]),
        ({"conversation_id": c1}, list(reversed(found[:2]))),
        ({"conversation_id": foreign_id}, []),
    ]:
        body = _body(
            client.get(
                "/api/me/messages/search", params={"q": f"  {marker}  ", **params}, headers=ctx["ordinary"]
            )
        )
        _equal(set(body), {"count", "query", "results"}, "self_search_shape")
        _equal((body["count"], body["query"]), (len(expected), marker), "self_search_count_query")
        wanted = [
            {key: row[key] for key in ("id", "conversation_id", "role", "content", "created_at")}
            for row in expected
        ]
        _equal(body["results"], wanted, "self_search_exact_results")
    for headers, query in [
        (ctx["owner"], marker),
        (ctx["ordinary"], " "),
        (ctx["ordinary"], "rtenmissinglocator"),
    ]:
        _equal(
            _body(client.get("/api/me/messages/search", params={"q": query}, headers=headers)),
            {"count": 0, "query": query.strip(), "results": []},
            "self_search_empty_or_unowned",
        )


def test_self_search_binds_query_limit_conversation_filter_and_actual_message_ids(self_http):
    _assert_search(self_http)


def _assert_reset(ctx):
    storage, client, person, foreign = ctx["storage"], ctx["client"], *_PEOPLE[:2]
    for channel, channel_id, conversation in (
        ("api", "other-channel", ctx["conversations"][person][1]),
        ("telegram", "same-channel", ctx["conversations"][person][2]),
    ):
        storage.set_channel_conversation(person, channel, channel_id, conversation["id"])
    foreign_session = copy.deepcopy(storage.get_channel_session(foreign, "api", "same-channel"))
    expected_sessions = [
        dict(row)
        for row in storage.execute(
            "SELECT * FROM channel_sessions ORDER BY user_id, channel, channel_id"
        ).fetchall()
        if (row["user_id"], row["channel"], row["channel_id"]) != (person, "api", "same-channel")
    ]
    conversations = [
        dict(row) for row in storage.execute("SELECT * FROM conversations ORDER BY id").fetchall()
    ]
    messages = [dict(row) for row in storage.execute("SELECT * FROM messages ORDER BY rowid").fetchall()]
    for cleared in (True, False):
        response = client.post(
            "/api/conversations/channel/reset", json={"channel_id": "same-channel"}, headers=ctx["ordinary"]
        )
        _equal(_body(response), {"status": "reset", "cleared": cleared}, "self_reset_http")
        assert storage.get_channel_session(person, "api", "same-channel") is None, "self_reset_persisted"
        _equal(
            storage.get_channel_session(foreign, "api", "same-channel"),
            foreign_session,
            "self_reset_foreign_session",
        )
        _equal(
            [
                dict(row)
                for row in storage.execute(
                    "SELECT * FROM channel_sessions ORDER BY user_id, channel, channel_id"
                ).fetchall()
            ],
            expected_sessions,
            "self_reset_keeps_other_sessions",
        )
    _equal(
        [dict(row) for row in storage.execute("SELECT * FROM messages ORDER BY rowid").fetchall()],
        messages,
        "self_reset_keeps_history",
    )
    assert (
        client.post("/api/conversations/channel/reset", json={}, headers=ctx["ordinary"]).status_code == 400
    )
    _equal(
        [dict(row) for row in storage.execute("SELECT * FROM conversations ORDER BY id").fetchall()],
        conversations,
        "self_reset_keeps_conversations",
    )


def test_self_reset_clears_only_own_channel_and_never_deletes_history(self_http):
    _assert_reset(self_http)


@pytest.mark.parametrize("missing_selector", ["user_id", "channel", "channel_id"])
def test_self_reset_oracle_rejects_actual_overbroad_session_deletion(
    self_http, monkeypatch, missing_selector
):
    storage = self_http["storage"]

    def overbroad_clear(user_id, channel, channel_id):
        # Damage the write reached by the real authenticated HTTP request.
        selectors = {"user_id": user_id, "channel": channel, "channel_id": channel_id}
        del selectors[missing_selector]
        where = " AND ".join(f"{name}=?" for name in selectors)
        with storage.transaction() as connection:
            result = connection.execute(
                f"DELETE FROM channel_sessions WHERE {where}", tuple(selectors.values())
            )
        return result.rowcount > 0

    monkeypatch.setattr(storage, "clear_channel_conversation", overbroad_clear)
    code = (
        "self_reset_foreign_session" if missing_selector == "user_id" else "self_reset_keeps_other_sessions"
    )
    with pytest.raises(AssertionError, match=code):
        _assert_reset(self_http)


def _assert_why(ctx):
    storage, client, person = ctx["storage"], ctx["client"], _PEOPLE[0]
    raw = RawObject(
        id=new_id("raw"),
        user_id=LEGACY_OWNER_USER_ID,
        source="test",
        source_ref="r10-why",
        raw_content="shared knowledge fixture",
        content_type="text",
    )
    storage.store_raw_object(raw)
    ko = KnowledgeObject(
        id=new_id("ko"),
        user_id=LEGACY_OWNER_USER_ID,
        raw_object_id=raw.id,
        title="Frozen shared title",
        content="shared knowledge fixture",
        summary="fixture",
    )
    storage.store_knowledge_object(ko)
    cid = ctx["conversations"][person][0]["id"]
    metadata = {
        "search_query": "frozen own question",
        "answer_mode": "personal_knowledge",
        "knowledge_hits": 2,
        "answer_grounded": True,
        "retrieval_confidence": 0.125,
        "knowledge_citations": {"K1": ko.id, "K2": _PRIVATE},
        "retrieval_trace": [
            {
                "id": ko.id,
                "title": _PRIVATE,
                "score": 0.5,
                "status": "returned",
                "reason": "insufficient_evidence",
                "private_path": _PRIVATE,
            },
            {"id": _PRIVATE, "title": _PRIVATE},
        ],
        "private_path": _PRIVATE,
    }
    assistant = storage.store_message(cid, person, "assistant", "owned answer", metadata=metadata)
    storage.store_message(cid, person, "user", "later user turn is not an answer")
    foreign_cid = ctx["conversations"][_PEOPLE[1]][0]["id"]
    storage.store_message(
        foreign_cid,
        _PEOPLE[1],
        "assistant",
        "foreign answer",
        metadata={"search_query": "foreign diagnostic canary"},
    )
    body = _body(
        client.get(
            "/api/conversations/channel/why", params={"channel_id": "same-channel"}, headers=ctx["ordinary"]
        )
    )
    expected = {
        "conversation_id": cid,
        "created_at": assistant["created_at"],
        "search_query": "frozen own question",
        "answer_mode": "personal_knowledge",
        "knowledge_hits": 2,
        "answer_grounded": True,
        "retrieval_confidence": 0.125,
        "citations": {"K1": ko.id},
        "trace": [
            {
                "id": ko.id,
                "title": "Frozen shared title",
                "score": 0.5,
                "status": "returned",
                "reason": "insufficient_evidence",
            }
        ],
    }
    _equal(body, expected, "self_why_owned_projection")
    assert _PRIVATE not in json.dumps(body) and "foreign diagnostic canary" not in json.dumps(body), (
        "self_why_private"
    )
    for params, status in [({}, 400), ({"channel_id": "missing-channel"}, 404)]:
        assert (
            client.get("/api/conversations/channel/why", params=params, headers=ctx["ordinary"]).status_code
            == status
        )
    assert (
        client.get(
            "/api/conversations/channel/why", params={"channel_id": "same-channel"}, headers=ctx["owner"]
        ).status_code
        == 404
    )


def test_self_why_selects_own_latest_assistant_and_resolves_shared_knowledge_safely(self_http):
    _assert_why(self_http)


def _assert_self_mutation(ctx, action):
    client, storage, person = ctx["client"], ctx["storage"], _PEOPLE[0]
    cid = ctx["conversations"][person][0]["id"]
    foreign_id = ctx["conversations"][_PEOPLE[1]][0]["id"]
    foreign = copy.deepcopy(storage.get_conversation(foreign_id, _PEOPLE[1]))
    url = f"/api/conversations/{cid}"
    if action == "rename":
        body = _body(client.patch(url, json={"title": "  new   own title  "}, headers=ctx["ordinary"]))
        _equal(
            (body["conversation"]["id"], body["conversation"]["title"]),
            (cid, "new own title"),
            "self_rename_http",
        )
        _equal(storage.get_conversation(cid, person)["title"], "new own title", "self_rename_persisted")
        assert client.patch(url, json={"title": "  "}, headers=ctx["ordinary"]).status_code == 400
    elif action == "archive":
        for archived in (True, False):
            body = _body(client.post(url + "/archive", json={"archived": archived}, headers=ctx["ordinary"]))
            _equal(
                (body["conversation"]["id"], body["conversation"]["is_archived"]),
                (cid, int(archived)),
                "self_archive_http",
            )
            _equal(
                storage.get_conversation(cid, person)["is_archived"], int(archived), "self_archive_persisted"
            )
            assert storage.get_channel_session(person, "api", "same-channel") is None, (
                "self_archive_clears_channel"
            )
    else:
        for _ in range(2):
            body = _body(client.delete(url, headers=ctx["ordinary"]))
            _equal(
                (
                    body["status"],
                    body["report"]["existed"],
                    body["report"]["archived"],
                    body["report"]["messages_kept"],
                ),
                ("archived", True, True, 2),
                "self_delete_http",
            )
            _equal(storage.get_conversation(cid, person)["is_archived"], 1, "self_delete_persisted")
            assert storage.get_channel_session(person, "api", "same-channel") is None, (
                "self_delete_clears_channel"
            )
    _equal(
        [row["id"] for row in storage.get_conversation_messages(cid, user_id=person)],
        [row["id"] for row in ctx["messages"][person][:2]],
        "self_mutation_keeps_history",
    )
    saved = copy.deepcopy(storage.get_conversation(cid, person))
    method, suffix = {"rename": ("PATCH", ""), "archive": ("POST", "/archive"), "delete": ("DELETE", "")}[
        action
    ]
    for bad_id in (foreign_id, "conv_r10_missing"):
        response = client.request(
            method,
            f"/api/conversations/{bad_id}" + suffix,
            json={"title": "bad", "archived": True},
            headers=ctx["ordinary"],
        )
        assert response.status_code == 404
    _equal(storage.get_conversation(cid, person), saved, "self_mutation_refusal_preserves_own")
    _equal(
        storage.get_conversation(foreign_id, _PEOPLE[1]), foreign, "self_mutation_refusal_preserves_foreign"
    )


@pytest.mark.parametrize("action", ["rename", "archive", "delete"])
def test_self_mutation_preserves_person_boundary_history_and_channel_state(self_http, action):
    _assert_self_mutation(self_http, action)


def test_current_reference_requires_signed_bridge_and_binds_literal_person_channel(self_http):
    ctx = self_http
    storage, client, settings = ctx["storage"], ctx["client"], ctx["settings"]
    person = "telegram:telegram:5001"
    storage.ensure_user(person, preset_key="user")
    conv = storage.create_conversation(person, "signed personal conversation")
    message = storage.store_message(conv["id"], person, "user", "signed own message")
    storage.set_channel_conversation(person, "telegram", "5001", conv["id"])
    _equal(
        _body(_bridge_get(client, settings, "/api/conversations/current/messages"))["items"][0]["id"],
        message["id"],
        "current_signed_history",
    )
    renamed = _body(
        _bridge_json(client, settings, "PATCH", "/api/conversations/current", {"title": "signed renamed"})
    )
    _equal(
        (renamed["conversation"]["id"], renamed["conversation"]["user_id"], renamed["conversation"]["title"]),
        (conv["id"], person, "signed renamed"),
        "current_signed_rename",
    )
    _equal(
        storage.get_conversation(conv["id"], person)["title"], "signed renamed", "current_signed_persisted"
    )
    for headers in (ctx["ordinary"], ctx["owner"]):
        assert client.get("/api/conversations/current/messages", headers=headers).status_code == 404
        assert (
            client.patch("/api/conversations/current", json={"title": "bad"}, headers=headers).status_code
            == 404
        )
    assert (
        _bridge_get(
            client, settings, "/api/conversations/current/messages", user="5002", chat="5002"
        ).status_code
        == 404
    )
    exported = _bridge_get(client, settings, "/api/conversations/current/export")
    _equal(exported.status_code, 200, "current_export_status")
    _equal(
        exported.content,
        _export_text({**conv, "title": "signed renamed"}, [message]).encode(),
        "current_export_bytes",
    )
    archived = _body(
        _bridge_json(client, settings, "POST", "/api/conversations/current/archive", {"archived": True})
    )
    _equal(
        (archived["conversation"]["id"], archived["conversation"]["is_archived"]),
        (conv["id"], 1),
        "current_archive_http",
    )
    _equal(storage.get_conversation(conv["id"], person)["is_archived"], 1, "current_archive_persisted")
    assert storage.get_channel_session(person, "telegram", "5001") is None, "current_archive_channel"
    assert _bridge_get(client, settings, "/api/conversations/current/messages").status_code == 404
    unarchived = _body(
        _bridge_json(
            client, settings, "POST", f"/api/conversations/{conv['id']}/archive", {"archived": False}
        )
    )
    _equal(unarchived["conversation"]["is_archived"], 0, "current_unarchive_by_id")
    storage.set_channel_conversation(person, "telegram", "5001", conv["id"])
    deleted = _body(_bridge_json(client, settings, "DELETE", "/api/conversations/current", {}))
    _equal((deleted["status"], deleted["report"]["messages_kept"]), ("archived", 1), "current_delete_http")
    _equal(storage.get_conversation(conv["id"], person)["is_archived"], 1, "current_delete_persisted")
    assert storage.get_channel_session(person, "telegram", "5001") is None, "current_delete_channel"
    _equal(
        [row["id"] for row in storage.get_conversation_messages(conv["id"], user_id=person)],
        [message["id"]],
        "current_history_retained",
    )


def test_anonymous_self_conversation_calls_have_no_personal_state_effect(self_http):
    ctx, person = self_http, _PEOPLE[0]
    cid = ctx["conversations"][person][0]["id"]
    before = copy.deepcopy(ctx["storage"].get_conversation(cid, person))
    session = copy.deepcopy(ctx["storage"].get_channel_session(person, "api", "same-channel"))
    routes = [
        ("GET", "/api/conversations"),
        ("GET", f"/api/conversations/{cid}/messages"),
        ("GET", f"/api/conversations/{cid}/export"),
        ("GET", "/api/me/messages/search"),
        ("GET", "/api/conversations/channel/why"),
        ("POST", "/api/conversations/channel/reset"),
        ("POST", f"/api/conversations/{cid}/archive"),
        ("PATCH", f"/api/conversations/{cid}"),
        ("DELETE", f"/api/conversations/{cid}"),
    ]
    for method, url in routes:
        assert (
            ctx["client"]
            .request(method, url, json={"title": "bad", "archived": True, "channel_id": "same-channel"})
            .status_code
            == 401
        )
    _equal(ctx["storage"].get_conversation(cid, person), before, "anonymous_conversation_unchanged")
    _equal(
        ctx["storage"].get_channel_session(person, "api", "same-channel"),
        session,
        "anonymous_channel_unchanged",
    )


_SELF_FAULTS = (
    ("listing_foreign", "self_list_members"),
    ("history_foreign", "chat_message_members"),
    ("export_wrong_bytes", "self_export_bytes"),
    ("search_foreign", "self_search_exact_results"),
    ("search_no_conversation", "self_search_count_query"),
    ("reset_unwritten", "self_reset_persisted"),
    ("reset_foreign", "self_reset_persisted"),
    ("why_wrong_query", "self_why_owned_projection"),
    ("why_wrong_title", "self_why_owned_projection"),
    ("rename_unwritten", "self_rename_persisted"),
    ("archive_unarchive_unwritten", "self_archive_persisted"),
    ("delete_unwritten", "self_delete_persisted"),
    ("current_foreign_message", "current_signed_history"),
)


@pytest.mark.parametrize("fault,code", _SELF_FAULTS, ids=[row[0] for row in _SELF_FAULTS])
def test_self_conversation_oracles_reject_cross_person_results_and_unperformed_changes(
    self_http, monkeypatch, fault, code
):
    ctx, request = self_http, TestClient.request
    storage = ctx["storage"]

    def damaged_request(self, method, url, **kwargs):
        response = request(self, method, url, **kwargs)
        if response.status_code != 200:
            return response
        path = str(url).split("?")[0]
        if fault == "export_wrong_bytes" and path.endswith("/export"):
            return httpx.Response(
                200,
                content=response.content.replace(b"first line", b"wrong line"),
                headers=response.headers,
                request=response.request,
            )
        if fault == "listing_foreign" and path == "/api/conversations":
            body = response.json()
            body["items"][0] = ctx["conversations"][_PEOPLE[1]][0]
        elif fault == "history_foreign" and path.endswith("/messages"):
            body = response.json()
            body["items"][0]["id"] = ctx["messages"][_PEOPLE[1]][0]["id"]
        elif fault == "search_foreign" and path == "/api/me/messages/search":
            body = response.json()
            if not body["results"]:
                return response
            body["results"][0]["id"] = ctx["messages"][_PEOPLE[1]][0]["id"]
        elif fault == "why_wrong_query" and path == "/api/conversations/channel/why":
            body = response.json()
            body["search_query"] = "foreign diagnostic canary"
        elif fault == "current_foreign_message" and path == "/api/conversations/current/messages":
            body = response.json()
            body["items"][0]["id"] = ctx["messages"][_PEOPLE[1]][0]["id"]
        else:
            return response
        return httpx.Response(200, json=body, request=response.request)

    monkeypatch.setattr(TestClient, "request", damaged_request)
    if fault == "search_no_conversation":
        search = storage.search_messages

        def unscoped_search(person, query, **kwargs):
            kwargs.pop("conversation_id", None)
            return search(person, query, **kwargs)

        monkeypatch.setattr(storage, "search_messages", unscoped_search)
    elif fault == "reset_unwritten":
        monkeypatch.setattr(storage, "clear_channel_conversation", lambda *args: True)
    elif fault == "reset_foreign":
        clear = storage.clear_channel_conversation
        monkeypatch.setattr(
            storage,
            "clear_channel_conversation",
            lambda person, channel, channel_id: clear(_PEOPLE[1], channel, channel_id),
        )
    elif fault == "why_wrong_title":
        get = storage.get_knowledge_object

        def fake_title(*args, **kwargs):
            row = get(*args, **kwargs)
            return {**row, "title": _PRIVATE} if row else row

        monkeypatch.setattr(storage, "get_knowledge_object", fake_title)
    elif fault == "rename_unwritten":
        monkeypatch.setattr(
            storage,
            "set_conversation_title",
            lambda cid, person, title: {**storage.get_conversation(cid, person), "title": title},
        )
    elif fault == "archive_unarchive_unwritten":
        archive = storage.set_conversation_archived

        def no_unarchive(cid, person, value):
            return (
                archive(cid, person, value)
                if value
                else {**storage.get_conversation(cid, person), "is_archived": 0}
            )

        monkeypatch.setattr(storage, "set_conversation_archived", no_unarchive)
    elif fault == "delete_unwritten":
        monkeypatch.setattr(
            storage,
            "delete_conversation",
            lambda cid, person: {
                "existed": True,
                "conversation_id": cid,
                "archived": True,
                "messages_kept": 2,
            },
        )
    scenario = {
        "listing": _assert_listing,
        "history": _assert_history,
        "export": _assert_export,
        "search": _assert_search,
        "reset": _assert_reset,
        "why": _assert_why,
        "rename": lambda value: _assert_self_mutation(value, "rename"),
        "archive": lambda value: _assert_self_mutation(value, "archive"),
        "delete": lambda value: _assert_self_mutation(value, "delete"),
        "current": test_current_reference_requires_signed_bridge_and_binds_literal_person_channel,
    }[fault.split("_")[0]]
    with pytest.raises(AssertionError, match=code):
        scenario(ctx)
