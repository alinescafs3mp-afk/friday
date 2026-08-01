"""Version diff — §8: a system that never loses history can also show it.

Pins the pure snapshot diff (scalar before→after, unified text diff, tag
add/remove, metadata key-level), the storage method's version selection
(default = two most recent), and the admin endpoint.
"""

from __future__ import annotations

import hashlib

from fastapi.testclient import TestClient

from friday.permissions import LEGACY_OWNER_USER_ID
from friday.server import create_app
from friday.storage.models import KnowledgeObject, RawObject, new_id
from friday.versions import diff_snapshots

# --- pure diff ------------------------------------------------------------


def test_diff_snapshots_by_field_kind():
    old = {
        "title": "IP Atlas",
        "summary": "Atlas 10.0.0.5",
        "content": "line one\nline two",
        "importance": 0.5,
        "lifecycle_stage": "active",
        "tags_json": '["net", "old"]',
        "metadata_json": '{"a": 1, "keep": "x"}',
    }
    new = {
        "title": "IP Atlas (обновлён)",
        "summary": "Atlas 10.0.0.7",
        "content": "line one\nline two changed",
        "importance": 0.5,  # unchanged
        "lifecycle_stage": "deprecated",
        "tags_json": '["net", "verified"]',
        "metadata_json": '{"a": 2, "keep": "x", "b": 3}',
    }
    changes = diff_snapshots(old, new)

    assert "importance" not in changes  # unchanged scalar is omitted
    assert changes["title"]["kind"] == "scalar"
    assert changes["title"]["to"] == "IP Atlas (обновлён)"
    assert changes["lifecycle_stage"]["to"] == "deprecated"

    assert changes["content"]["kind"] == "text"
    assert "line two changed" in changes["content"]["unified"]
    assert changes["content"]["unified"].startswith("---")

    assert changes["tags"] == {"kind": "set", "added": ["verified"], "removed": ["old"]}

    meta = changes["metadata"]
    assert meta["added"] == {"b": 3}
    assert meta["changed"] == {"a": {"from": 1, "to": 2}}
    assert meta["removed"] == {}


def test_diff_snapshots_no_changes_is_empty():
    same = {"title": "x", "tags_json": "[]", "metadata_json": "{}"}
    assert diff_snapshots(same, dict(same)) == {}


# --- storage + endpoint ---------------------------------------------------


def _seed(storage, user_id: str) -> str:
    content = "Сервер Atlas имеет IP 10.0.0.5."
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
        title="Atlas IP",
        summary=content,
        tags_json=["net"],
    )
    storage.store_knowledge_object(ko)
    return ko.id


def test_diff_defaults_to_two_most_recent(storage):
    storage.ensure_user("alice")
    ko_id = _seed(storage, "alice")  # version 1
    storage.update_knowledge_fields(ko_id, "alice", title="Atlas IP (v2)")  # version 2
    storage.update_knowledge_fields(ko_id, "alice", lifecycle_stage="deprecated")  # version 3

    result = storage.diff_knowledge_versions(ko_id, "alice")
    assert result["from_version"] == 2
    assert result["to_version"] == 3
    assert result["available_versions"] == [1, 2, 3]
    assert result["changes"]["lifecycle_stage"]["to"] == "deprecated"

    # Explicit range diffs across the whole history.
    full = storage.diff_knowledge_versions(ko_id, "alice", from_version=1, to_version=3)
    assert full["changes"]["title"]["from"] == "Atlas IP"
    assert full["changes"]["title"]["to"] == "Atlas IP (v2)"
    assert full["changes"]["lifecycle_stage"]["to"] == "deprecated"


def test_diff_single_version_has_no_changes(storage):
    storage.ensure_user("alice")
    ko_id = _seed(storage, "alice")
    result = storage.diff_knowledge_versions(ko_id, "alice")
    assert result["from_version"] == result["to_version"] == 1
    assert result["changes"] == {}


def test_diff_endpoint(settings):
    app = create_app(settings)
    with TestClient(app) as client:
        owner = {"Authorization": f"Bearer {settings.api_token}"}
        ko_id = _seed(app.state.storage, LEGACY_OWNER_USER_ID)
        app.state.storage.update_knowledge_fields(ko_id, LEGACY_OWNER_USER_ID, title="Изменён")

        response = client.get(
            f"/api/admin/knowledge/{ko_id}/diff",
            params={"user_id": LEGACY_OWNER_USER_ID},
            headers=owner,
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["from_version"] == 1 and body["to_version"] == 2
        assert body["changes"]["title"]["to"] == "Изменён"

        missing = client.get(
            "/api/admin/knowledge/ko_missing/diff",
            params={"user_id": LEGACY_OWNER_USER_ID},
            headers=owner,
        )
        assert missing.status_code == 404
