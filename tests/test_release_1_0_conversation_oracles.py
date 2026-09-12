"""Exact administrative conversation HTTP outcomes in a model-free installation."""

from __future__ import annotations

import copy
import hashlib
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from friday.permissions import LEGACY_OWNER_USER_ID
from friday.server import create_app
from friday.storage import _conversations as conversation_store
from friday.storage.models import RawObject, new_id

_PEOPLE = ("r10-chat-a", "r10-chat-b", "r10-chat-c")
_SECRET = "jrc_" + "R10ChatPreview9_-" * 4
_PRIVATE = "/private/r10-conversation-canary"


def _equal(actual, expected, code):
    assert actual == expected, code


def _audit(storage, action):
    return [
        dict(row)
        for row in storage.execute(
            "SELECT * FROM audit_log WHERE action=? ORDER BY rowid", (action,)
        ).fetchall()
    ]


def _body(response):
    assert response.status_code == 200, response.text
    return response.json()


@pytest.fixture
def conversation_http(settings, monkeypatch):
    assert not settings.llm_enabled and not settings.workers_enabled
    app = create_app(settings)
    with TestClient(app) as client:
        owner = {"Authorization": f"Bearer {settings.api_token}"}
        storage = app.state.storage
        conversations, messages = {}, {}
        for person in _PEOPLE:
            storage.ensure_user(
                person,
                preset_key="user",
                metadata={"chat_id": f"synthetic-{person}", "private_path": _PRIVATE},
            )
            conversations[person], messages[person] = [], []
        token = _body(client.post("/api/admin/tokens", headers=owner, json={"user_id": _PEOPLE[0]}))["token"]
        ordinary = {"Authorization": f"Bearer {token}"}
        # Freeze only the fixture clock. All HTTP handlers and storage reads use
        # their real implementation; no app/chat/model path constructs expected output.
        tick = 0

        def clock():
            nonlocal tick
            tick += 1
            return (datetime(2024, 1, 1, tzinfo=UTC) + timedelta(seconds=tick)).isoformat()

        with monkeypatch.context() as seed:
            seed.setattr(conversation_store, "utc_now", clock)
            for person in _PEOPLE:
                for group in range(3):
                    conv = storage.create_conversation(person, f"{person} conversation {group}")
                    conversations[person].append(conv)
                    for index in range(2):
                        content = f"{person} message {group}:{index}"
                        if group == 2 and index == 1:
                            content += f" {_SECRET}"
                        message = storage.store_message(
                            conv["id"],
                            person,
                            "user" if index == 0 else "assistant",
                            content,
                            metadata={"private_path": _PRIVATE},
                        )
                        messages[person].append(message)
        # File attribution is distinct from shared storage tenancy.
        for index, author in enumerate((_PEOPLE[0], _PEOPLE[0], _PEOPLE[1], None)):
            metadata = {} if author is None else {"uploaded_by": author}
            text = f"synthetic file {index}"
            storage.store_raw_object(
                RawObject(
                    id=new_id("raw"),
                    user_id=LEGACY_OWNER_USER_ID,
                    source="test",
                    source_ref=f"r10-chat-{index}",
                    raw_content=text,
                    content_type="file",
                    content_hash=hashlib.sha256(text.encode()).hexdigest(),
                    metadata_json=metadata,
                )
            )
        yield {
            "client": client,
            "storage": storage,
            "owner": owner,
            "ordinary": ordinary,
            "conversations": conversations,
            "messages": messages,
        }


def _assert_feed(ctx):
    storage, client = ctx["storage"], ctx["client"]
    before = _audit(storage, "admin.chat_feed.read")
    body = _body(client.get("/api/admin/chats", params={"limit": 2}, headers=ctx["owner"]))
    _equal(set(body), {"items", "count", "shown", "files_without_an_author"}, "chat_feed_shape")
    _equal((body["count"], body["shown"], body["files_without_an_author"]), (3, 2, 1), "chat_feed_totals")
    _equal([row["user_id"] for row in body["items"]], [_PEOPLE[2], _PEOPLE[1]], "chat_feed_members")
    for row, person, files in zip(body["items"], reversed(_PEOPLE[1:]), (0, 1), strict=True):
        _equal(
            set(row),
            {
                "user_id",
                "display_name",
                "username",
                "preset_key",
                "status",
                "chat_id",
                "last_content",
                "last_role",
                "last_at",
                "last_conversation_id",
                "message_count",
                "file_count",
            },
            "chat_feed_projection",
        )
        _equal(
            (row["message_count"], row["file_count"], row["chat_id"]),
            (6, files, f"synthetic-{person}"),
            "chat_feed_attribution",
        )
        last = ctx["messages"][person][-1]
        _equal(
            (row["last_content"], row["last_role"], row["last_at"], row["last_conversation_id"]),
            (
                f"{person} message 2:1 [redacted:token]",
                "assistant",
                last["created_at"],
                ctx["conversations"][person][-1]["id"],
            ),
            "chat_feed_last_message",
        )
    assert _SECRET not in json.dumps(body) and _PRIVATE not in json.dumps(body), "chat_feed_privacy"
    added = _audit(storage, "admin.chat_feed.read")[len(before) :]
    _equal(len(added), 1, "chat_feed_audit_count")
    _equal(
        (added[0]["target_id"], json.loads(added[0]["after_json"])),
        ("*", {"limit": 2, "scope": "all_tenants"}),
        "chat_feed_audit",
    )


def test_admin_chat_feed_has_exact_http_members_attribution_and_audit(conversation_http):
    _assert_feed(conversation_http)


def _assert_cursor(ctx):
    client, storage = ctx["client"], ctx["storage"]
    before = storage.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
    expected = {"total": 18, "last_at": ctx["messages"][_PEOPLE[-1]][-1]["created_at"]}
    _equal(
        _body(client.get("/api/admin/chats/cursor", headers=ctx["owner"])), expected, "chat_cursor_initial"
    )
    conv = ctx["conversations"][_PEOPLE[0]][0]["id"]
    message = storage.store_message(conv, _PEOPLE[0], "user", "new cursor observation")
    _equal(
        _body(client.get("/api/admin/chats/cursor", headers=ctx["owner"])),
        {"total": 19, "last_at": message["created_at"]},
        "chat_cursor_advance",
    )
    _equal(
        storage.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0],
        before,
        "chat_cursor_no_content_audit",
    )


def test_admin_chat_cursor_tracks_actual_insert_and_has_no_content_audit(conversation_http):
    _assert_cursor(conversation_http)


def _assert_messages(body, expected, *, insights=False):
    fields = {"id", "role", "content", "created_at"} | ({"insights"} if insights else set())
    _equal([item["id"] for item in body["items"]], [row["id"] for row in expected], "chat_message_members")
    for item, row in zip(body["items"], expected, strict=True):
        _equal(set(item), fields, "chat_message_projection")
        _equal(
            (item["role"], item["content"], item["created_at"]),
            (row["role"], row["content"].replace(_SECRET, "[redacted:token]"), row["created_at"]),
            "chat_message_content",
        )
    assert _PRIVATE not in json.dumps(body) and _SECRET not in json.dumps(body), "chat_message_privacy"


def _assert_thread(ctx):
    person = _PEOPLE[0]
    before = _audit(ctx["storage"], "admin.messages.read")
    body = _body(
        ctx["client"].get(f"/api/admin/chats/{person}/messages", params={"limit": 3}, headers=ctx["owner"])
    )
    _equal(set(body), {"user_id", "items", "count", "total", "limit"}, "chat_thread_shape")
    _equal(
        (body["user_id"], body["count"], body["total"], body["limit"]),
        (person, 3, 6, 3),
        "chat_thread_totals",
    )
    _assert_messages(body, ctx["messages"][person][-3:])
    added = _audit(ctx["storage"], "admin.messages.read")[len(before) :]
    _equal(len(added), 1, "chat_thread_audit_count")
    _equal(added[0]["target_id"], person, "chat_thread_audit_target")
    # The audit enum hides arbitrary scope strings. User and action still bind
    # the observed read; only the private string's shape is retained.
    _equal(json.loads(added[0]["after_json"]), {"scope_chars": 11}, "chat_thread_audit_projection")
    assert ctx["client"].get("/api/admin/chats/r10-missing/messages", headers=ctx["owner"]).status_code == 404


def test_admin_person_thread_spans_conversations_with_exact_http_window(conversation_http):
    _assert_thread(conversation_http)


def _assert_conversation_pages(ctx):
    person, client, storage = _PEOPLE[0], ctx["client"], ctx["storage"]
    conversations = ctx["conversations"][person]
    # Archive one fixture before observing both default and inclusive windows.
    storage.set_conversation_archived(conversations[2]["id"], person, True)
    before = _audit(storage, "admin.conversations.read")
    for include, expected in [
        (False, [conversations[1], conversations[0]]),
        (True, [conversations[2], conversations[1], conversations[0]]),
    ]:
        for offset in (0, 1, len(expected)):
            body = _body(
                client.get(
                    "/api/admin/conversations",
                    params={
                        "user_id": person,
                        "include_archived": str(include).lower(),
                        "limit": 1,
                        "offset": offset,
                    },
                    headers=ctx["owner"],
                )
            )
            _equal(
                set(body),
                {"user_id", "items", "count", "total", "limit", "offset"},
                "conversation_page_shape",
            )
            _equal(
                (body["user_id"], body["total"], body["limit"], body["offset"], body["count"]),
                (person, len(expected), 1, offset, int(offset < len(expected))),
                "conversation_page_totals",
            )
            _equal(
                [row["id"] for row in body["items"]],
                [row["id"] for row in expected[offset : offset + 1]],
                "conversation_page_members",
            )
            assert all(row["user_id"] == person for row in body["items"]), "conversation_page_tenant"
    added = _audit(storage, "admin.conversations.read")[len(before) :]
    _equal(len(added), 6, "conversation_page_audit_count")
    assert all(row["target_id"] == person for row in added), "conversation_page_audit_target"


def test_admin_conversation_pages_bind_filter_archive_offset_and_exact_total(conversation_http):
    _assert_conversation_pages(conversation_http)


def _assert_conversation_messages(ctx):
    person, client, storage = _PEOPLE[0], ctx["client"], ctx["storage"]
    cid = ctx["conversations"][person][-1]["id"]
    url = f"/api/admin/conversations/{cid}/messages"
    before = _audit(storage, "admin.messages.read")
    for offset, expected in [
        (None, ctx["messages"][person][-1:]),
        (0, ctx["messages"][person][-2:-1]),
        (2, []),
    ]:
        params = {"user_id": person, "limit": 1}
        if offset is not None:
            params["offset"] = offset
        body = _body(client.get(url, params=params, headers=ctx["owner"]))
        _equal(
            set(body),
            {"conversation_id", "items", "count", "total", "limit", "offset"},
            "conversation_messages_shape",
        )
        _equal(
            (body["conversation_id"], body["count"], body["total"], body["limit"], body["offset"]),
            (cid, len(expected), 2, 1, 1 if offset is None else offset),
            "conversation_messages_totals",
        )
        _assert_messages(body, expected, insights=True)
    added = _audit(storage, "admin.messages.read")[len(before) :]
    _equal(len(added), 3, "conversation_messages_audit_count")
    assert all(
        row["target_id"] == person and json.loads(row["after_json"])["conversation_id"] == cid
        for row in added
    ), "conversation_messages_audit_binding"
    assert client.get(url, params={"user_id": _PEOPLE[1]}, headers=ctx["owner"]).status_code == 404


def test_admin_conversation_transcript_has_exact_tail_offset_and_tenant_binding(conversation_http):
    _assert_conversation_messages(conversation_http)


def _assert_reply(ctx):
    person, storage, client = _PEOPLE[0], ctx["storage"], ctx["client"]
    before = _audit(storage, "admin.chat.reply")
    assert storage.execute("SELECT COUNT(*) FROM outbound_notifications").fetchone()[0] == 0
    for _ in range(2):
        body = _body(
            client.post(
                f"/api/admin/chats/{person}/reply",
                json={"text": "  synthetic owner response  "},
                headers=ctx["owner"],
            )
        )
        _equal(body, {"queued": True, "user_id": person, "chat_id": f"synthetic-{person}"}, "chat_reply_http")
    rows = [
        dict(row) for row in storage.execute("SELECT * FROM outbound_notifications ORDER BY rowid").fetchall()
    ]
    _equal(len(rows), 2, "chat_reply_persisted_count")
    assert len({row["id"] for row in rows}) == len({row["dedup_key"] for row in rows}) == 2, (
        "chat_reply_independent_requests"
    )
    for row in rows:
        _equal(
            (row["user_id"], row["chat_id"], row["kind"], row["body"], row["status"], row["attempts"]),
            (
                person,
                f"synthetic-{person}",
                "owner_reply",
                "💬 Ответ от владельца:\n\nsynthetic owner response",
                "pending",
                0,
            ),
            "chat_reply_persisted_content",
        )
    added = _audit(storage, "admin.chat.reply")[len(before) :]
    _equal(len(added), 2, "chat_reply_audit_count")
    for row in added:
        _equal(row["target_id"], person, "chat_reply_audit_target")
        _equal(
            json.loads(row["after_json"]),
            {"chars": 24, "chat_id_present": True, "queued": True, "by_chars": 36},
            "chat_reply_audit_content",
        )
    storage.ensure_user("r10-no-chat")
    for who, text, status in [
        (person, " ", 400),
        (person, "x" * 4001, 400),
        ("r10-missing", "hello", 404),
        ("r10-no-chat", "hello", 409),
    ]:
        assert (
            client.post(
                f"/api/admin/chats/{who}/reply", json={"text": text}, headers=ctx["owner"]
            ).status_code
            == status
        )
    _equal(
        [
            dict(row)
            for row in storage.execute("SELECT * FROM outbound_notifications ORDER BY rowid").fetchall()
        ],
        rows,
        "chat_reply_refusals_no_effect",
    )
    _equal(_audit(storage, "admin.chat.reply"), before + added, "chat_reply_refusals_no_success_audit")


def test_admin_reply_persists_two_exact_owned_queue_items_and_refusals_have_no_effect(conversation_http):
    _assert_reply(conversation_http)


def _assert_mutation(ctx, action):
    client, storage, person = ctx["client"], ctx["storage"], _PEOPLE[0]
    cid = ctx["conversations"][person][0]["id"]
    foreign = copy.deepcopy(storage.get_conversation(ctx["conversations"][_PEOPLE[1]][0]["id"], _PEOPLE[1]))
    storage.set_channel_conversation(person, "telegram", "synthetic-archive", cid)
    before = _audit(storage, f"admin.conversation.{action}")
    url = f"/api/admin/conversations/{cid}"
    if action == "archive":
        body = _body(
            client.post(
                url + "/archive", params={"user_id": person}, json={"archived": True}, headers=ctx["owner"]
            )
        )
        _equal(
            (
                body["conversation"]["id"],
                body["conversation"]["user_id"],
                body["conversation"]["is_archived"],
            ),
            (cid, person, 1),
            "conversation_archive_http",
        )
    else:
        body = _body(client.delete(url, params={"user_id": person}, headers=ctx["owner"]))
        _equal(body["status"], "deleted", "conversation_delete_http")
        _equal(
            (body["report"]["existed"], body["report"]["archived"], body["report"]["messages_kept"]),
            (True, True, 2),
            "conversation_delete_report",
        )
    _equal(storage.get_conversation(cid, person)["is_archived"], 1, "conversation_mutation_persisted")
    _equal(
        [row["id"] for row in storage.get_conversation_messages(cid, user_id=person)],
        [row["id"] for row in ctx["messages"][person][:2]],
        "conversation_mutation_keeps_history",
    )
    assert storage.get_channel_session(person, "telegram", "synthetic-archive") is None, (
        "conversation_mutation_clears_channel"
    )
    added = _audit(storage, f"admin.conversation.{action}")[len(before) :]
    _equal(len(added), 1, "conversation_mutation_audit_count")
    _equal(added[0]["target_id"], cid, "conversation_mutation_audit_target")
    after = json.loads(added[0]["after_json"])
    if action == "archive":
        # Stored is_archived is an integer, hidden by the audit's boolean
        # allowlist. Actual persisted state is independently asserted above.
        _equal(after, {"private_fields_count": 1}, "conversation_archive_audit")
        unarchived = _body(
            client.post(
                url + "/archive", params={"user_id": person}, json={"archived": False}, headers=ctx["owner"]
            )
        )
        _equal(unarchived["conversation"]["is_archived"], 0, "conversation_unarchive_http")
        _equal(storage.get_conversation(cid, person)["is_archived"], 0, "conversation_unarchive_persisted")
    else:
        _equal(
            after,
            {"conversation_id": cid, "archived": True, "private_fields_count": 4, "private_items_count": 1},
            "conversation_delete_audit",
        )
    _equal(
        storage.get_conversation(foreign["id"], _PEOPLE[1]),
        foreign,
        "conversation_mutation_foreign_unchanged",
    )
    prior_state = storage.get_conversation(cid, person)
    prior_audit = _audit(storage, f"admin.conversation.{action}")
    for target_id, target_user in [(cid, _PEOPLE[1]), ("conv_r10_missing", person)]:
        target_url = f"/api/admin/conversations/{target_id}"
        if action == "archive":
            response = client.post(
                target_url + "/archive",
                params={"user_id": target_user},
                json={"archived": True},
                headers=ctx["owner"],
            )
        else:
            response = client.delete(target_url, params={"user_id": target_user}, headers=ctx["owner"])
        assert response.status_code == 404
    _equal(storage.get_conversation(cid, person), prior_state, "conversation_refusal_no_state_change")
    _equal(
        _audit(storage, f"admin.conversation.{action}"), prior_audit, "conversation_refusal_no_success_audit"
    )


@pytest.mark.parametrize("action", ["archive", "delete"])
def test_admin_archive_delete_observe_persistence_history_channel_and_audit(conversation_http, action):
    _assert_mutation(conversation_http, action)


def test_admin_conversation_http_refuses_ordinary_anonymous_and_invalid_windows(conversation_http):
    ctx = conversation_http
    client, person = ctx["client"], _PEOPLE[0]
    cid = ctx["conversations"][person][0]["id"]
    routes = [
        ("GET", "/api/admin/chats"),
        ("GET", "/api/admin/chats/cursor"),
        ("GET", f"/api/admin/chats/{person}/messages"),
        ("POST", f"/api/admin/chats/{person}/reply"),
        ("GET", "/api/admin/conversations"),
        ("GET", f"/api/admin/conversations/{cid}/messages"),
        ("POST", f"/api/admin/conversations/{cid}/archive"),
        ("DELETE", f"/api/admin/conversations/{cid}"),
    ]
    for headers, status in [({}, 401), (ctx["ordinary"], 403)]:
        for method, url in routes:
            response = client.request(
                method,
                url,
                params={"user_id": person},
                json={"text": "unauthorized", "archived": True},
                headers=headers,
            )
            assert response.status_code == status, (method, url, response.text)
    assert ctx["storage"].execute("SELECT COUNT(*) FROM outbound_notifications").fetchone()[0] == 0
    assert ctx["storage"].get_conversation(cid, person)["is_archived"] == 0
    for _, url in [row for row in routes if row[0] == "GET" and "cursor" not in row[1]]:
        assert (
            client.get(url, params={"user_id": person, "limit": 0}, headers=ctx["owner"]).status_code == 422
        )


_FAULTS = (
    ("feed_members", "chat_feed_members"),
    ("feed_total", "chat_feed_totals"),
    ("cursor_stale", "chat_cursor_advance"),
    ("thread_member", "chat_message_members"),
    ("thread_private", "chat_message_projection"),
    ("page_total", "conversation_page_totals"),
    ("page_tenant", "conversation_page_members"),
    ("transcript_offset", "conversation_messages_totals"),
    ("reply_false", "chat_reply_http"),
    ("reply_missing", "chat_reply_persisted_count"),
    ("reply_foreign", "chat_reply_persisted_content"),
    ("archive_unwritten", "conversation_mutation_persisted"),
    ("delete_unwritten", "conversation_mutation_persisted"),
    ("delete_audit_missing", "conversation_mutation_audit_count"),
)


@pytest.mark.parametrize("fault,code", _FAULTS, ids=[row[0] for row in _FAULTS])
def test_conversation_oracles_reject_mutated_http_results_and_unperformed_effects(
    conversation_http, monkeypatch, fault, code
):
    ctx, requests = conversation_http, TestClient.request
    storage = ctx["storage"]

    def damaged_request(self, method, url, **kwargs):
        response = requests(self, method, url, **kwargs)
        if response.status_code != 200:
            return response
        path = str(url).split("?")[0]
        body = response.json()
        if fault.startswith("feed_") and path == "/api/admin/chats":
            if fault == "feed_members":
                body["items"] = list(reversed(body["items"]))
            else:
                body["count"] = body["shown"]
        elif fault == "cursor_stale" and path == "/api/admin/chats/cursor":
            body["total"] = 18
        elif fault.startswith("thread_") and path == f"/api/admin/chats/{_PEOPLE[0]}/messages":
            if fault == "thread_member":
                body["items"][0]["id"] = ctx["messages"][_PEOPLE[1]][0]["id"]
            else:
                body["items"][0]["metadata_json"] = {"private_path": _PRIVATE}
        elif fault.startswith("page_") and path == "/api/admin/conversations":
            if fault == "page_total":
                body["total"] = body["count"]
            elif body["items"]:
                body["items"][0] = ctx["conversations"][_PEOPLE[1]][0]
        elif fault == "transcript_offset" and path.endswith("/messages"):
            body["offset"] = 0
        elif fault == "reply_false" and path.endswith("/reply"):
            body["queued"] = False
        else:
            return response
        return httpx.Response(response.status_code, json=body, request=response.request)

    monkeypatch.setattr(TestClient, "request", damaged_request)
    if fault == "reply_missing":
        monkeypatch.setattr(storage, "enqueue_notification", lambda *args, **kwargs: True)
    elif fault == "reply_foreign":
        enqueue = storage.enqueue_notification

        def wrong_recipient(_user, _chat, body, **kwargs):
            return enqueue(_PEOPLE[1], f"synthetic-{_PEOPLE[1]}", body, **kwargs)

        monkeypatch.setattr(storage, "enqueue_notification", wrong_recipient)
    elif fault == "archive_unwritten":

        def unwritten_archive(cid, user, archived):
            return {**storage.get_conversation(cid, user), "is_archived": int(archived)}

        monkeypatch.setattr(storage, "set_conversation_archived", unwritten_archive)
    elif fault == "delete_unwritten":
        monkeypatch.setattr(
            storage,
            "delete_conversation",
            lambda cid, user: {
                "existed": True,
                "conversation_id": cid,
                "archived": True,
                "messages_kept": 2,
                "deleted": {"channel_sessions": 1},
                "cancelled": {"work_items": 0},
            },
        )
    elif fault == "delete_audit_missing":
        from friday.admin_api import _conversations as routes

        audit = routes._audit

        def drop_delete_audit(request, action, *args, **kwargs):
            if action != "admin.conversation.delete":
                return audit(request, action, *args, **kwargs)

        monkeypatch.setattr(routes, "_audit", drop_delete_audit)
    scenario = {
        "feed": _assert_feed,
        "cursor": _assert_cursor,
        "thread": _assert_thread,
        "page": _assert_conversation_pages,
        "transcript": _assert_conversation_messages,
        "reply": _assert_reply,
        "archive": lambda value: _assert_mutation(value, "archive"),
        "delete": lambda value: _assert_mutation(value, "delete"),
    }[fault.split("_")[0]]
    with pytest.raises(AssertionError, match=code):
        scenario(ctx)
