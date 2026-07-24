"""Scoped per-account API tokens make the role model reachable over HTTP.

Historically the only HTTP auth paths (owner bearer token, loopback) both resolved
to the owner with every capability, so presets/roles applied to Telegram users only.
These tests pin: a scoped token authenticates as its bound account with exactly that
account's preset (not owner), the owner token still yields the owner, revoked/unknown
tokens are rejected, minting is capability-gated, and a delegated admin cannot mint an
owner token (no privilege escalation).
"""

from __future__ import annotations

import hashlib

from fastapi.testclient import TestClient

from jericho.permissions import LEGACY_OWNER_USER_ID
from jericho.server import create_app


def _issue(storage, user_id: str, preset: str, secret: str) -> dict:
    storage.ensure_user(user_id, source="api-token", display_name=user_id, preset_key=preset)
    storage.update_user(user_id, preset_key=preset)
    token_hash = hashlib.sha256(secret.encode("utf-8")).hexdigest()
    return storage.create_api_token(user_id, token_hash, label="test", created_by="test")


# --- storage layer --------------------------------------------------------


def test_token_store_hashes_metadata_only_and_revokes(storage):
    storage.ensure_user("u1", preset_key="user")
    token_hash = hashlib.sha256(b"jrc_secret").hexdigest()
    record = storage.create_api_token("u1", token_hash, label="phone", created_by="test")

    found = storage.find_api_token(token_hash)
    assert found and found["user_id"] == "u1"
    storage.touch_api_token(record["id"])

    listed = storage.list_api_tokens("u1")
    assert listed and listed[0]["id"] == record["id"]
    # The hash/secret is never exposed through the listing.
    assert "token_sha256" not in listed[0]

    assert storage.revoke_api_token(record["id"]) is True
    assert storage.find_api_token(token_hash) is None
    assert storage.revoke_api_token(record["id"]) is False


# --- auth resolution ------------------------------------------------------


def test_scoped_token_authenticates_as_bound_account_not_owner(settings):
    app = create_app(settings)
    with TestClient(app) as client:
        _issue(app.state.storage, "mod1", "moderator", "jrc_moderator_secret")
        scoped = {"Authorization": "Bearer jrc_moderator_secret"}

        me = client.get("/api/me", headers=scoped)
        assert me.status_code == 200
        assert me.json()["actor"]["user_id"] == "mod1"
        assert me.json()["actor"]["preset_key"] == "moderator"

        # A moderator lacks admin capabilities, so admin-only routes are denied.
        assert client.get("/api/admin/overview", headers=scoped).status_code == 403
        assert client.post("/api/admin/tokens", json={"user_id": "mod1"}, headers=scoped).status_code == 403

        # The configured owner token still resolves to the all-capability owner.
        owner = {"Authorization": f"Bearer {settings.api_token}"}
        owner_me = client.get("/api/me", headers=owner)
        assert owner_me.json()["actor"]["preset_key"] == "owner"
        assert client.get("/api/admin/overview", headers=owner).status_code == 200


def test_revoked_and_unknown_tokens_are_rejected(settings):
    app = create_app(settings)
    with TestClient(app) as client:
        record = _issue(app.state.storage, "mod2", "moderator", "jrc_will_revoke")
        assert client.get("/api/me", headers={"Authorization": "Bearer jrc_will_revoke"}).status_code == 200

        app.state.storage.revoke_api_token(record["id"])
        assert client.get("/api/me", headers={"Authorization": "Bearer jrc_will_revoke"}).status_code == 401
        assert client.get("/api/me", headers={"Authorization": "Bearer jrc_never_issued"}).status_code == 401


# --- minting endpoint -----------------------------------------------------


def test_owner_mints_scoped_token_that_authenticates_then_revokes(settings):
    app = create_app(settings)
    with TestClient(app) as client:
        owner = {"Authorization": f"Bearer {settings.api_token}"}
        storage = app.state.storage
        storage.ensure_user("mod3", source="admin", preset_key="moderator")
        storage.update_user("mod3", preset_key="moderator")

        created = client.post("/api/admin/tokens", json={"user_id": "mod3", "label": "laptop"}, headers=owner)
        assert created.status_code == 200
        secret = created.json()["token"]
        token_id = created.json()["id"]
        assert secret.startswith("jrc_")

        me = client.get("/api/me", headers={"Authorization": f"Bearer {secret}"})
        assert me.json()["actor"]["user_id"] == "mod3"

        assert client.delete(f"/api/admin/tokens/{token_id}", headers=owner).status_code == 200
        assert client.get("/api/me", headers={"Authorization": f"Bearer {secret}"}).status_code == 401


def test_delegated_admin_cannot_mint_owner_token(settings):
    app = create_app(settings)
    with TestClient(app) as client:
        storage = app.state.storage
        _issue(storage, "adm1", "admin", "jrc_admin_secret")
        admin = {"Authorization": "Bearer jrc_admin_secret"}

        # An admin may mint a token for a normal account…
        storage.ensure_user("user9", source="admin", preset_key="user")
        storage.update_user("user9", preset_key="user")
        assert client.post("/api/admin/tokens", json={"user_id": "user9"}, headers=admin).status_code == 200

        # …but not for the owner account (privilege escalation is refused).
        escalation = client.post("/api/admin/tokens", json={"user_id": LEGACY_OWNER_USER_ID}, headers=admin)
        assert escalation.status_code == 403
