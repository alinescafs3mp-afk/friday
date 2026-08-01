"""Conversations gain a lifecycle: archive/unarchive and delete.

Previously conversations had no user- or admin-reachable archive or delete, so chat
history accumulated forever. These tests pin archive visibility, cascade deletion
(messages + their feedback + channel binding), and the user/API surface.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from friday.permissions import LEGACY_OWNER_USER_ID
from friday.server import create_app
from friday.storage.models import FeedbackItem, FeedbackType, new_id


def _count(storage, sql: str, params: tuple) -> int:
    row = storage.execute(sql, params).fetchone()
    return int(row[0]) if row else 0


def test_archive_and_unarchive_toggle_default_visibility(storage):
    storage.ensure_user("alice")
    conv = storage.create_conversation("alice", "Chat", mode="dialogue")
    cid = conv["id"]
    assert [c["id"] for c in storage.list_conversations("alice")] == [cid]

    storage.set_conversation_archived(cid, "alice", True)
    assert storage.list_conversations("alice") == []
    assert [c["id"] for c in storage.list_conversations("alice", include_archived=True)] == [cid]

    updated = storage.set_conversation_archived(cid, "alice", False)
    assert updated is not None and updated["is_archived"] == 0
    assert [c["id"] for c in storage.list_conversations("alice")] == [cid]


def test_delete_conversation_cascades_messages_feedback_and_channel_binding(storage):
    storage.ensure_user("alice")
    conv = storage.create_conversation("alice", "Chat")
    cid = conv["id"]
    message = storage.store_message(cid, "alice", "assistant", "Ответ [K1].")
    mid = message["id"]
    storage.store_feedback(
        FeedbackItem(
            id=new_id("fb"),
            user_id="alice",
            target_type="answer",
            target_id=mid,
            feedback_type=FeedbackType.ANSWER_USEFULNESS,
            score=1.0,
        )
    )
    storage.set_channel_conversation("alice", "telegram", "5001", cid, mode="dialogue")

    assert storage.get_conversation_messages(cid, user_id="alice")
    assert _count(storage, "SELECT COUNT(*) FROM feedback WHERE target_id=?", (mid,)) == 1
    assert _count(storage, "SELECT COUNT(*) FROM feedback_state WHERE target_id=?", (mid,)) == 1
    assert storage.get_channel_session("alice", "telegram", "5001") is not None

    report = storage.delete_conversation(cid, "alice")
    assert report["existed"] is True
    assert report["deleted"]["conversations"] == 1
    assert report["deleted"]["messages"] == 1

    assert storage.get_conversation(cid, "alice") is None
    assert _count(storage, "SELECT COUNT(*) FROM messages WHERE conversation_id=?", (cid,)) == 0
    assert _count(storage, "SELECT COUNT(*) FROM feedback WHERE target_id=?", (mid,)) == 0
    assert _count(storage, "SELECT COUNT(*) FROM feedback_state WHERE target_id=?", (mid,)) == 0
    assert storage.get_channel_session("alice", "telegram", "5001") is None

    assert storage.delete_conversation(cid, "alice")["existed"] is False


def test_conversations_manage_capability_scoped_to_real_users(storage):
    from friday.permissions import CORE_CAPABILITIES, ActorContext, AuthorizationService

    cap = next(c for c in CORE_CAPABILITIES if c.security_id == "conversations.manage")
    assert cap.default_presets == ("admin", "moderator", "user")
    auth = AuthorizationService(storage)

    def _allowed(preset: str) -> bool:
        return auth.authorize(
            ActorContext(user_id=f"u-{preset}", preset_key=preset, source="test"),
            "conversations.manage",
        ).allowed

    assert _allowed("user") is True
    assert _allowed("moderator") is True
    assert _allowed("guest") is False


def test_user_archives_then_deletes_own_conversation_over_http(settings):
    app = create_app(settings)
    with TestClient(app) as client:
        owner = {"Authorization": f"Bearer {settings.api_token}"}
        conv = app.state.storage.create_conversation(LEGACY_OWNER_USER_ID, "Chat")
        cid = conv["id"]

        archived = client.post(f"/api/conversations/{cid}/archive", json={"archived": True}, headers=owner)
        assert archived.status_code == 200
        assert archived.json()["conversation"]["is_archived"] == 1
        listed = client.get("/api/conversations", headers=owner).json()["items"]
        assert cid not in [c["id"] for c in listed]

        deleted = client.delete(f"/api/conversations/{cid}", headers=owner)
        assert deleted.status_code == 200
        assert deleted.json()["status"] == "deleted"
        assert client.get(f"/api/conversations/{cid}/messages", headers=owner).status_code == 404
        assert client.delete(f"/api/conversations/{cid}", headers=owner).status_code == 404


def test_set_conversation_title_updates_own_row_only(storage):
    """Mutation: drop the UPDATE in set_conversation_title — title stays old, red."""
    storage.ensure_user("alice")
    storage.ensure_user("bob")
    own = storage.create_conversation("alice", "старое")
    foreign = storage.create_conversation("bob", "чужое")

    updated = storage.set_conversation_title(own["id"], "alice", "  новое имя  ")
    assert updated is not None
    assert updated["title"] == "новое имя"
    assert storage.get_conversation(own["id"], "alice")["title"] == "новое имя"

    # Foreign id + own user_id → silent miss (404 at HTTP), never bob's row.
    assert storage.set_conversation_title(foreign["id"], "alice", "взлом") is None
    assert storage.get_conversation(foreign["id"], "bob")["title"] == "чужое"
    assert storage.set_conversation_title(own["id"], "alice", "   ") is None


def test_rename_and_current_sentinel_are_self_service_only(settings):
    """G18: PATCH title; `current` only for telegram-bridge channel session.

    Mutation: remove set_conversation_title call from PATCH → title stays, red.
    Foreign conversation id → 404 (not 403), same as archive/delete.
    """
    from dataclasses import replace

    from tests.test_api_vertical_slice import _bridge_json, _bridge_request

    scoped = replace(settings, telegram_allowed_chat_ids=[5001, 5002], telegram_owner_chat_ids=[])
    app = create_app(scoped)
    with TestClient(app) as client:
        owner = {"Authorization": f"Bearer {scoped.api_token}"}
        # Owner renames own conversation by real id.
        conv = app.state.storage.create_conversation(LEGACY_OWNER_USER_ID, "было")
        cid = conv["id"]
        renamed = client.patch(
            f"/api/conversations/{cid}",
            json={"title": "стало"},
            headers=owner,
        )
        assert renamed.status_code == 200, renamed.text
        assert renamed.json()["conversation"]["title"] == "стало"
        assert app.state.storage.get_conversation(cid, LEGACY_OWNER_USER_ID)["title"] == "стало"

        empty = client.patch(f"/api/conversations/{cid}", json={"title": "  "}, headers=owner)
        assert empty.status_code == 400

        # Seed alice (5001) and bob (5002) conversations via chat.
        alice_chat = _bridge_request(
            client,
            scoped,
            "/api/chat",
            {
                "message": "привет alice",
                "source_ref": "telegram-update:ren1",
                "telegram_message_id": 1,
                "telegram_user": {"id": 5001},
            },
            user="5001",
            chat="5001",
        )
        assert alice_chat.status_code == 200, alice_chat.text
        alice_cid = alice_chat.json()["conversation_id"]

        bob_chat = _bridge_request(
            client,
            scoped,
            "/api/chat",
            {
                "message": "привет bob",
                "source_ref": "telegram-update:ren2",
                "telegram_message_id": 1,
                "telegram_user": {"id": 5002},
            },
            user="5002",
            chat="5002",
        )
        assert bob_chat.status_code == 200, bob_chat.text
        bob_cid = bob_chat.json()["conversation_id"]
        bob_row = app.state.storage.execute(
            "SELECT user_id, title FROM conversations WHERE id=?", (bob_cid,)
        ).fetchone()
        assert bob_row is not None
        bob_user_id = str(bob_row["user_id"])
        bob_title_before = str(bob_row["title"])

        # Alice renames current chat via sentinel.
        via_current = _bridge_json(
            client,
            scoped,
            "PATCH",
            "/api/conversations/current",
            {"title": "alice-current"},
            user="5001",
            chat="5001",
        )
        assert via_current.status_code == 200, via_current.text
        assert via_current.json()["conversation"]["title"] == "alice-current"
        assert via_current.json()["conversation"]["id"] == alice_cid

        # Alice cannot rename bob's conversation — 404, not 403.
        foreign = _bridge_json(
            client,
            scoped,
            "PATCH",
            f"/api/conversations/{bob_cid}",
            {"title": "украдено"},
            user="5001",
            chat="5001",
        )
        assert foreign.status_code == 404
        assert app.state.storage.get_conversation(bob_cid, bob_user_id)["title"] == bob_title_before

        # Archive current for alice.
        archived = _bridge_json(
            client,
            scoped,
            "POST",
            "/api/conversations/current/archive",
            {"archived": True},
            user="5001",
            chat="5001",
        )
        assert archived.status_code == 200, archived.text
        assert archived.json()["conversation"]["is_archived"] == 1

        # Delete foreign → 404.
        steal_delete = _bridge_json(
            client,
            scoped,
            "DELETE",
            f"/api/conversations/{bob_cid}",
            {},
            user="5001",
            chat="5001",
        )
        assert steal_delete.status_code == 404
        assert app.state.storage.get_conversation(bob_cid, bob_user_id) is not None
