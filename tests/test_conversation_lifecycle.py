"""Conversations gain a lifecycle: archive/unarchive and delete.

Previously conversations had no user- or admin-reachable archive or delete, so chat
history accumulated forever. These tests pin archive visibility, cascade deletion
(messages + their feedback + channel binding), and the user/API surface.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from jericho.permissions import LEGACY_OWNER_USER_ID
from jericho.server import create_app
from jericho.storage.models import FeedbackItem, FeedbackType, new_id


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
    from jericho.permissions import CORE_CAPABILITIES, ActorContext, AuthorizationService

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
