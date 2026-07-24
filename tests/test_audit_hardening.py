"""Audit-log completeness and append-only enforcement — §20.

Data egress (file/backup/export downloads) and audit-trail reads previously
left no audit rows, and nothing but convention prevented rewriting audit
history. These tests pin the database-level append-only triggers, egress
auditing, and the cross-tenant read policy (another account's content — logged;
one's own — not).
"""

from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

from jericho.server import create_app


def _actions(storage) -> list[str]:
    return [row["action"] for row in storage.list_audit_log(None, limit=100)]


def test_audit_log_is_append_only_at_database_level(storage):
    storage.ensure_user("alice")
    from jericho.storage.models import AuditEntry, new_id

    entry = storage.log_audit(
        AuditEntry(
            id=new_id("audit"),
            user_id="alice",
            action="test.action",
            target_type="t",
            target_id="x",
        )
    )
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        storage.execute("UPDATE audit_log SET action='forged' WHERE id=?", (entry.id,))
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        storage.execute("DELETE FROM audit_log WHERE id=?", (entry.id,))
    assert _actions(storage) == ["test.action"]


def test_downloads_and_audit_reads_are_audited(settings):
    app = create_app(settings)
    with TestClient(app) as client:
        owner = {"Authorization": f"Bearer {settings.api_token}"}

        made = client.post("/api/admin/backups", json={"label": "t"}, headers=owner)
        assert made.status_code == 200
        name = made.json()["backup"]["database"]
        assert client.get(f"/api/admin/backups/{name}/download", headers=owner).status_code == 200

        exported = client.post("/api/admin/exports", json={}, headers=owner)
        assert exported.status_code == 200
        filename = exported.json()["export"]["filename"]
        assert client.get(f"/api/admin/exports/{filename}/download", headers=owner).status_code == 200

        assert client.get("/api/admin/audit", headers=owner).status_code == 200

        actions = _actions(app.state.storage)
        for expected in (
            "admin.backup.download",
            "admin.export.download",
            "admin.audit.read",
        ):
            assert expected in actions, actions


def test_cross_tenant_reads_are_audited_own_reads_are_not(settings):
    app = create_app(settings)
    with TestClient(app) as client:
        owner = {"Authorization": f"Bearer {settings.api_token}"}
        storage = app.state.storage
        storage.ensure_user("local:kate", source="test", display_name="Kate")

        # Owner reading their own data: no audit noise.
        assert client.get("/api/admin/knowledge", headers=owner).status_code == 200
        assert "admin.knowledge.read" not in _actions(storage)

        # Owner reading another account's content: recorded with the target.
        assert (
            client.get("/api/admin/knowledge", params={"user_id": "local:kate"}, headers=owner).status_code
            == 200
        )
        rows = storage.list_audit_log(None, limit=50)
        match = [row for row in rows if row["action"] == "admin.knowledge.read"]
        assert match and match[0]["target_id"] == "local:kate"

        assert (
            client.get("/api/admin/inbox", params={"user_id": "local:kate"}, headers=owner).status_code == 200
        )
        assert "admin.inbox.read" in _actions(storage)
