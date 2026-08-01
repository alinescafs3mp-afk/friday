"""Looking into another account's missions has to obey the same rules as everything else.

The admin mission router was the one place where cross-tenant access skipped the two
guards every other admin route passes through.

* `GET /api/admin/missions` logged a cross-tenant read; `GET /api/admin/missions/{id}`
  — the endpoint that returns the goal the user wrote and the text every step produced
  — logged nothing. The listing left a trail, the content did not.
* `POST /api/admin/missions/{id}/cancel` had neither `_protect_owner_target` nor an
  audit entry, so a delegated administrator could stop the owner's own missions and
  nothing recorded who did it. Token revocation was hardened against exactly this;
  missions were missed.
"""

from __future__ import annotations

import hashlib

from fastapi.testclient import TestClient

from friday.permissions import LEGACY_OWNER_USER_ID
from friday.server import create_app


def _issue(storage, user_id: str, preset: str, secret: str) -> dict:
    storage.ensure_user(user_id, source="api-token", display_name=user_id, preset_key=preset)
    storage.update_user(user_id, preset_key=preset)
    return storage.create_api_token(
        user_id, hashlib.sha256(secret.encode()).hexdigest(), label="test", created_by="test"
    )


def _mission_of(client, headers, goal: str) -> str:
    created = client.post("/api/missions", headers=headers, json={"goal": goal})
    assert created.status_code == 200, created.text
    return created.json()["mission"]["id"]


def test_reading_another_accounts_mission_is_recorded(settings):
    app = create_app(settings)
    with TestClient(app) as client:
        storage = app.state.storage
        _issue(storage, "adm", "admin", "jrc_admin_reader")
        admin = {"Authorization": "Bearer jrc_admin_reader"}
        owner = {"Authorization": f"Bearer {settings.api_token}"}

        mission_id = _mission_of(client, owner, "Личная цель владельца")

        before = len(storage.list_audit_log("adm", limit=200))
        detail = client.get(f"/api/admin/missions/{mission_id}", headers=admin)
        assert detail.status_code == 200
        assert detail.json()["mission"]["id"] == mission_id

        entries = storage.list_audit_log("adm", limit=200)
        assert len(entries) > before, "an admin read another account's mission and left no trace"
        assert any(entry["action"] == "admin.missions.read" for entry in entries)


def test_a_delegated_admin_cannot_cancel_the_owners_mission(settings):
    app = create_app(settings)
    with TestClient(app) as client:
        storage = app.state.storage
        _issue(storage, "adm2", "admin", "jrc_admin_canceller")
        admin = {"Authorization": "Bearer jrc_admin_canceller"}
        owner = {"Authorization": f"Bearer {settings.api_token}"}
        storage.ensure_user(LEGACY_OWNER_USER_ID, preset_key="owner")
        storage.update_user(LEGACY_OWNER_USER_ID, preset_key="owner")

        mission_id = _mission_of(client, owner, "Цель, которую нельзя отменить чужими руками")

        refused = client.post(f"/api/admin/missions/{mission_id}/cancel", headers=admin)
        assert refused.status_code == 403, refused.text
        still = client.get(f"/api/missions/{mission_id}", headers=owner).json()["mission"]
        assert still["status"] != "cancelled"


def test_cancelling_an_ordinary_accounts_mission_still_works_and_is_recorded(settings, storage):
    app = create_app(settings)
    with TestClient(app) as client:
        storage = app.state.storage
        _issue(storage, "adm3", "admin", "jrc_admin_ok")
        admin = {"Authorization": "Bearer jrc_admin_ok"}
        _issue(storage, "bob", "user", "jrc_bob")
        bob = {"Authorization": "Bearer jrc_bob"}

        mission_id = _mission_of(client, bob, "Обычная пользовательская цель")

        cancelled = client.post(f"/api/admin/missions/{mission_id}/cancel", headers=admin)
        assert cancelled.status_code == 200, cancelled.text
        assert cancelled.json()["mission"]["status"] == "cancelled"

        actions = [entry["action"] for entry in storage.list_audit_log("adm3", limit=200)]
        assert "admin.mission.cancel" in actions, f"the cancellation left no audit entry: {actions}"
