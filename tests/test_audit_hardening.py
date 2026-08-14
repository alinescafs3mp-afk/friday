"""Audit-log completeness and append-only enforcement — §20.

Data egress (file/backup/export downloads) and audit-trail reads previously
left no audit rows, and nothing but convention prevented rewriting audit
history. These tests pin the database-level append-only triggers, egress
auditing, and the cross-tenant read policy (another account's content — logged;
one's own — not).
"""

from __future__ import annotations

import re
import sqlite3

import pytest
from fastapi.testclient import TestClient

from friday.server import create_app

# Hard delete is deliberately absent from the live surface until its restore
# protocol is complete.  Its read-shaped preflight therefore fails closed before
# it can disclose (or audit) another account's deletion inventory.
_INTENTIONALLY_UNAVAILABLE_READS = {"/api/admin/users/{user_id}/deletion": 503}


def _actions(storage) -> list[str]:
    return [row["action"] for row in storage.list_audit_log(None, limit=100)]


def _seed_addressable_objects(storage, user_id: str) -> dict[str, str]:
    """One real row per path placeholder, so `/graph/{entity_id}` can be requested.

    Returns the mapping placeholder-name -> id. A placeholder with no entry makes
    the walk report the route as unreachable rather than skip it quietly.
    """
    import hashlib
    import pathlib

    from friday.storage.models import (
        Entity,
        EntityType,
        KnowledgeObject,
        Mission,
        RawObject,
        new_id,
    )

    raw = RawObject(
        id=new_id("raw"),
        user_id=user_id,
        source="test",
        source_ref=new_id("src"),
        raw_content="Материал для проверки аудита",
        content_type="text",
        content_hash=hashlib.sha256(b"audit-probe").hexdigest(),
    )
    storage.store_raw_object(raw)
    knowledge = KnowledgeObject(
        id=new_id("ko"),
        user_id=user_id,
        raw_object_id=raw.id,
        content="Материал для проверки аудита",
        content_type="text",
        title="Проба",
    )
    storage.store_knowledge_object(knowledge)
    entity = Entity(id=new_id("ent"), user_id=user_id, name="Проба", entity_type=EntityType.CONCEPT)
    storage.create_entity(entity)
    conversation = storage.create_conversation(user_id, "Проба")

    # A stored file, not just a raw row: `/files/{raw_id}/download` checks
    # `content_type == "file"` and resolves `stored_path` under `files_dir`.
    stored = pathlib.Path(storage.settings.files_dir) / user_id / "проба.txt"
    stored.parent.mkdir(parents=True, exist_ok=True)
    body = "байты".encode()
    stored.write_bytes(body)
    digest = hashlib.sha256(body).hexdigest()
    upload = RawObject(
        id=new_id("raw"),
        user_id=user_id,
        source="upload",
        source_ref=new_id("src"),
        raw_content="байты",
        content_type="file",
        content_hash=digest,
        metadata_json={
            "filename": "проба.txt",
            "stored_path": f"{user_id}/проба.txt",
            "sha256": digest,
            "size_bytes": len(body),
        },
    )
    storage.store_raw_object(upload)

    mission = Mission(id=new_id("mis"), user_id=user_id, goal="Проба")
    storage.create_mission(mission)

    return {
        "user_id": user_id,
        "raw_id": upload.id,
        "knowledge_id": knowledge.id,
        "entity_id": entity.id,
        "mission_id": mission.id,
        "conversation_id": str(conversation["id"] if isinstance(conversation, dict) else conversation.id),
    }


def test_audit_log_is_append_only_at_database_level(storage):
    storage.ensure_user("alice")
    from friday.storage.models import AuditEntry, new_id

    entry = storage.log_audit(
        AuditEntry(
            id=new_id("audit"),
            user_id="alice",
            action="monitor.create",
            target_type="monitor",
            target_id=new_id("mon"),
        )
    )
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        storage.execute("UPDATE audit_log SET action='forged' WHERE id=?", (entry.id,))
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        storage.execute("DELETE FROM audit_log WHERE id=?", (entry.id,))
    assert _actions(storage) == ["monitor.create"]


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


def test_every_admin_read_of_another_account_is_audited(settings, monkeypatch, tmp_path):
    """Inventory-shaped on purpose: a list of call sites drifts, an enumeration does not.

    `_audit_cross_tenant_read` existed and was wired into knowledge, graph, inbox,
    conversations and files — and not into eval, quality, retrieval-explain or
    lifecycle. Those four read another account's material and left no trace, which
    is the one thing the audit trail exists for. This walks the routes rather than
    naming them, so the next module extracted from admin_api cannot quietly repeat
    it.

    Parameterised paths were skipped at first, on the reasoning that `{entity_id}`
    cannot be requested literally. That exemption hid a real hole for months:
    `GET /api/admin/graph/{entity_id}` returned another account's subgraph — the
    names of their people, organisations and projects — with no row in the log.
    The exemption is gone; the walk now seeds a real object for each placeholder
    so the parameterised routes are actually exercised.
    """
    import inspect

    from friday.server import create_app

    def _admin_get_routes(app):
        """`include_router` stores an `_IncludedRouter` wrapper rather than flattening."""
        found: list[tuple[str, object]] = []

        def walk(routes, prefix: str) -> None:
            for route in routes:
                nested = getattr(route, "original_router", None)
                if nested is not None:
                    context = getattr(route, "include_context", None)
                    walk(nested.routes, prefix + getattr(context, "prefix", ""))
                    continue
                path = getattr(route, "path", None)
                if path is None or "GET" not in (getattr(route, "methods", None) or set()):
                    continue
                found.append((prefix + path, route.endpoint))

        walk(app.routes, "")
        return found

    app = create_app(settings)
    storage = None
    with TestClient(app) as client:
        storage = app.state.storage
        storage.ensure_user("victim", preset_key="user")
        placeholders = _seed_addressable_objects(storage, "victim")
        # Внешний источник чужого человека: чтобы дорога к его схеме отвечала
        # 200, нужна и запись об источнике, и настоящая база под переменной.
        external = tmp_path / "victim.sqlite3"
        probe = sqlite3.connect(external)
        probe.execute("CREATE TABLE staff (id INTEGER PRIMARY KEY)")
        probe.commit()
        probe.close()
        monkeypatch.setenv("VICTIM_PROBE_DSN", str(external))
        storage.register_data_source(
            "victim", name="probe", kind="sqlite", dsn_env="VICTIM_PROBE_DSN", created_by="victim"
        )
        placeholders["name"] = "probe"
        owner = {"Authorization": f"Bearer {settings.api_token}"}

        checked: list[str] = []
        unaudited: list[str] = []
        unreachable: list[str] = []
        unavailable_seen: list[str] = []

        def audit_count() -> int:
            row = storage.execute("SELECT COUNT(*) AS c FROM audit_log").fetchone()
            return int(row["c"] if row else 0)

        for path, endpoint in _admin_get_routes(app):
            if not path.startswith("/api/admin/"):
                continue
            if "user_id" not in inspect.signature(endpoint).parameters:
                continue

            concrete = path
            missing = False
            for name in re.findall(r"\{(\w+)\}", path):
                if name not in placeholders:
                    missing = True
                    break
                concrete = concrete.replace("{" + name + "}", placeholders[name])
            if missing:
                unreachable.append(path)
                continue

            before = audit_count()
            response = client.get(concrete, params={"user_id": "victim", "q": "проба"}, headers=owner)
            if response.status_code >= 400:
                expected_status = _INTENTIONALLY_UNAVAILABLE_READS.get(path)
                if response.status_code == expected_status:
                    unavailable_seen.append(path)
                    continue
                # A route that could not be reached is not evidence of anything, so it
                # is reported rather than skipped: silence here is what let the graph
                # route sit unaudited.
                unreachable.append(f"{path} -> {response.status_code}")
                continue
            checked.append(path)
            if audit_count() == before:
                unaudited.append(path)

    assert checked, "no admin GET with a user_id parameter was reachable — the walk is broken"
    assert not unaudited, f"these read another account without an audit row: {unaudited}"
    assert any("{" in path for path in checked), (
        "no parameterised route was exercised — the seeding broke and the exemption is back"
    )
    assert set(unavailable_seen) == set(_INTENTIONALLY_UNAVAILABLE_READS), (
        "the explicit hard-delete preflight quarantine drifted: "
        f"expected {sorted(_INTENTIONALLY_UNAVAILABLE_READS)}, saw {sorted(unavailable_seen)}"
    )
    assert not unreachable, (
        "these admin reads could not be exercised, so nothing is known about their auditing "
        f"— seed an object for them or give them a reason to be here: {unreachable}"
    )
