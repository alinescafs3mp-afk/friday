"""Conflict resolution by action — a confirmed conflict actually gets settled.

Detection + confirmation only flag a contradiction; §5 adds the deciding action:
the user picks a winner, the loser becomes ``deprecated`` and points at the
winner (``superseded_by_id`` + metadata stamp), and the conflict flips to
``resolved``. Provenance is preserved (the loser is versioned, not deleted),
retrieval stops surfacing the loser as a current fact, and re-running is safe.
"""

from __future__ import annotations

import hashlib
import json

import pytest
from fastapi.testclient import TestClient

from jericho.permissions import LEGACY_OWNER_USER_ID
from jericho.server import create_app
from jericho.storage.models import KnowledgeObject, RawObject, new_id


def _store(storage, user_id: str, content: str, title: str) -> dict:
    raw = RawObject(
        id=new_id("raw"),
        user_id=user_id,
        source="test",
        source_ref=new_id("src"),
        raw_content=content,
        content_type="text",
        content_hash=hashlib.sha256(content.encode()).hexdigest(),
    )
    storage.store_raw_object(raw)
    ko = KnowledgeObject(
        id=new_id("ko"),
        user_id=user_id,
        raw_object_id=raw.id,
        content=content,
        content_type="text",
        title=title,
        summary=content,
    )
    storage.store_knowledge_object(ko)
    return storage.get_knowledge_object(ko.id, user_id) or {}


def _conflict(storage, user_id: str) -> tuple[dict, dict, str]:
    old = _store(storage, user_id, "Сервер Atlas имеет IP 10.0.0.5.", "Atlas IP (старый)")
    new = _store(storage, user_id, "Сервер Atlas имеет IP 10.0.0.7.", "Atlas IP (новый)")
    conflict = storage.store_knowledge_conflict(
        user_id, old["id"], new["id"], conflict_type="address_mismatch", confidence=0.9
    )
    return old, new, conflict["id"]


def test_resolve_deprecates_loser_and_links_to_winner(storage):
    storage.ensure_user("alice")
    old, new, conflict_id = _conflict(storage, "alice")

    result = storage.resolve_conflict("alice", conflict_id, new["id"], reviewed_by="alice")
    assert result["winner_id"] == new["id"]
    assert result["deprecated_id"] == old["id"]

    loser = storage.get_knowledge_object(old["id"], "alice")
    assert loser["lifecycle_stage"] == "deprecated"
    assert loser["superseded_by_id"] == new["id"]
    metadata = json.loads(loser["metadata_json"])
    assert metadata["deprecated_by_conflict"]["superseded_by"] == new["id"]
    assert metadata["deprecated_by_conflict"]["conflict_id"] == conflict_id
    # Provenance preserved: the loser gained a version snapshot, not a tombstone.
    assert loser["deleted_at"] is None
    assert int(loser["version"]) >= 2

    winner = storage.get_knowledge_object(new["id"], "alice")
    assert winner["lifecycle_stage"] == "active"

    conflict = storage.get_knowledge_conflict("alice", conflict_id)
    assert conflict["status"] == "resolved"
    assert new["id"] in conflict["resolution_note"]


def test_resolve_stops_the_loser_ranking_as_current(storage):
    storage.ensure_user("alice")
    old, new, conflict_id = _conflict(storage, "alice")
    # Both records match a search for the IP before resolution.
    before = {hit["id"] for hit in storage.search_knowledge("alice", "Atlas IP")}
    assert {old["id"], new["id"]} <= before

    storage.resolve_conflict("alice", conflict_id, new["id"], reviewed_by="alice")
    hits = storage.search_knowledge("alice", "Atlas IP")
    stages = {hit["id"]: hit["lifecycle_stage"] for hit in hits}
    # The loser is still findable (not deleted) but flagged deprecated so
    # retrieval + the agent no longer treat it as the current fact.
    assert stages.get(old["id"]) == "deprecated"
    assert stages.get(new["id"]) == "active"


def test_resolve_validates_winner_and_terminal_status(storage):
    storage.ensure_user("alice")
    old, new, conflict_id = _conflict(storage, "alice")

    with pytest.raises(ValueError, match="winner_id"):
        storage.resolve_conflict("alice", conflict_id, "ko_not_in_conflict", reviewed_by="alice")

    storage.resolve_conflict("alice", conflict_id, new["id"], reviewed_by="alice")
    # Re-resolving a settled conflict is refused (terminal), so a double-click
    # cannot flip the winner after the fact.
    with pytest.raises(ValueError, match="already resolved"):
        storage.resolve_conflict("alice", conflict_id, old["id"], reviewed_by="alice")


def test_resolve_endpoint_gated_audited_and_wired(settings):
    app = create_app(settings)
    with TestClient(app) as client:
        owner = {"Authorization": f"Bearer {settings.api_token}"}
        storage = app.state.storage
        old, new, conflict_id = _conflict(storage, LEGACY_OWNER_USER_ID)

        missing = client.post(
            f"/api/admin/conflicts/{conflict_id}/resolve",
            json={"user_id": LEGACY_OWNER_USER_ID},
            headers=owner,
        )
        assert missing.status_code == 400  # winner_id required

        response = client.post(
            f"/api/admin/conflicts/{conflict_id}/resolve",
            json={"user_id": LEGACY_OWNER_USER_ID, "winner_id": new["id"]},
            headers=owner,
        )
        assert response.status_code == 200, response.text
        assert response.json()["item"]["deprecated_id"] == old["id"]
        assert (
            storage.get_knowledge_object(old["id"], LEGACY_OWNER_USER_ID)["lifecycle_stage"] == "deprecated"
        )

        actions = [row["action"] for row in storage.list_audit_log(None, limit=50)]
        assert "admin.knowledge_conflict.resolve" in actions
