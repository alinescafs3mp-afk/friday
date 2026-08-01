"""A regular user could create and edit their own knowledge but never delete it.

Every mainstream consumer AI assistant lets a person forget a specific fact it
holds. `DELETE /api/knowledge/{id}` already existed, already soft-deletes (a
version snapshot survives — this is not destructive), and was already strictly
scoped to `actor.user_id` (404s on a foreign object, never a cross-tenant leak).
The gap was permissions-only: `knowledge.delete`'s grant tuple in
`CORE_CAPABILITIES` was `("admin", "moderator")` while its siblings on the same
objects — `knowledge.create`, `knowledge.edit` — already granted `"user"`. Found
by an independent research pass comparing this project against mainstream
consumer assistant capabilities, verified against the actual route and storage
code before fixing.
"""

from __future__ import annotations

import hashlib

from fastapi.testclient import TestClient

from friday.server import create_app
from friday.storage.models import KnowledgeObject, RawObject, new_id


def _issue(storage, user_id: str, preset: str, secret: str) -> dict:
    storage.ensure_user(user_id, source="api-token", display_name=user_id, preset_key=preset)
    storage.update_user(user_id, preset_key=preset)
    return storage.create_api_token(
        user_id, hashlib.sha256(secret.encode()).hexdigest(), label="test", created_by="test"
    )


def _make_ko(storage, user_id: str, content: str) -> str:
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
    knowledge = KnowledgeObject(
        id=new_id("ko"),
        user_id=user_id,
        raw_object_id=raw.id,
        content=content,
        content_type="text",
        title="T",
    )
    storage.store_knowledge_object(knowledge)
    return knowledge.id


def test_a_plain_user_can_delete_their_own_knowledge_object(settings, storage):
    _issue(storage, "alice", "user", "alice-secret")
    ko_id = _make_ko(storage, "alice", "Заметка Алисы")

    with TestClient(create_app(settings_override=settings)) as client:
        response = client.delete(f"/api/knowledge/{ko_id}", headers={"Authorization": "Bearer alice-secret"})

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "soft_deleted"
    assert storage.get_knowledge_object(ko_id, "alice").get("deleted_at")


def test_a_plain_user_still_cannot_delete_someone_elses_knowledge_object(settings, storage):
    _issue(storage, "alice", "user", "alice-secret-2")
    other_ko_id = _make_ko(storage, "bob", "Заметка Боба")

    with TestClient(create_app(settings_override=settings)) as client:
        response = client.delete(
            f"/api/knowledge/{other_ko_id}", headers={"Authorization": "Bearer alice-secret-2"}
        )

    assert response.status_code == 404
    assert not storage.get_knowledge_object(other_ko_id, "bob").get("deleted_at")
