"""Purge must not leave the purged text in the one table nothing can clean.

`audit_log` is append-only at the DATABASE level: BEFORE UPDATE and BEFORE DELETE
triggers `RAISE(ABORT)`, so whatever a route writes there is permanent beyond the
reach of any later fix, purge or redaction.

The admin routes audited the WHOLE row — `content` included — before deleting it.
So the one operation whose entire purpose is «destroy every trace of this object»
durably wrote that object's full text into the table that cannot be cleaned, and
the edit and delete routes had already deposited every earlier revision there.
The CLI path did it correctly (`before_json=None`); the HTTP path did not.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from friday.server import create_app

SECRET_TEXT = "Пароль от роутера 12345 и адрес квартиры, Лесная 7-15."


def _audit_blob(storage) -> str:
    rows = storage.execute("SELECT before_json, after_json FROM audit_log").fetchall()
    return json.dumps([dict(row) for row in rows], ensure_ascii=False)


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings)) as client:
        yield client


def _ingest(client, settings) -> tuple[str, str]:
    owner = {"Authorization": f"Bearer {settings.api_token}"}
    response = client.post(
        "/api/ingest", json={"content": SECRET_TEXT, "force_knowledge": True}, headers=owner
    )
    assert response.status_code == 200, response.text
    body = response.json()
    knowledge = body.get("knowledge_object") or {}
    assert knowledge.get("id"), body
    return str(knowledge["id"]), str(knowledge["user_id"])


def test_purge_leaves_no_body_behind(client, settings):
    owner = {"Authorization": f"Bearer {settings.api_token}"}
    knowledge_id, user_id = _ingest(client, settings)
    storage = client.app.state.storage

    edit = client.patch(
        f"/api/admin/knowledge/{knowledge_id}?user_id={user_id}",
        json={"user_id": user_id, "title": "Переименовано"},
        headers=owner,
    )
    assert edit.status_code == 200, edit.text
    assert (
        client.delete(f"/api/admin/knowledge/{knowledge_id}?user_id={user_id}", headers=owner).status_code
        == 200
    )
    purge = client.post(
        f"/api/admin/knowledge/{knowledge_id}/purge?user_id={user_id}", json={}, headers=owner
    )
    assert purge.status_code == 200, purge.text

    # The object is gone from the database…
    assert storage.get_knowledge_object(knowledge_id, user_id) is None
    # …and so is its text, including from the journal that nothing can rewrite.
    assert SECRET_TEXT not in _audit_blob(storage)


def test_the_journal_still_identifies_what_was_purged(client, settings):
    """Redaction must not turn the audit trail into an empty gesture."""
    owner = {"Authorization": f"Bearer {settings.api_token}"}
    knowledge_id, user_id = _ingest(client, settings)
    client.delete(f"/api/admin/knowledge/{knowledge_id}?user_id={user_id}", headers=owner)
    client.post(f"/api/admin/knowledge/{knowledge_id}/purge?user_id={user_id}", json={}, headers=owner)

    storage = client.app.state.storage
    row = storage.execute(
        "SELECT before_json, after_json FROM audit_log WHERE action='admin.knowledge.purge'"
    ).fetchone()
    assert row is not None, "the purge itself must still be recorded"
    before = json.loads(row["before_json"])
    assert before["id"] == knowledge_id
    assert before["content_chars"] == len(SECRET_TEXT)
    assert len(before["content_sha256"]) == 64  # provable identity without the text
