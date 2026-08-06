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
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from friday.cli import _parse_ttl_seconds
from friday.permissions import LEGACY_OWNER_USER_ID
from friday.server import create_app
from friday.storage import SCHEMA_VERSION, FridayStorage


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


def test_delegated_admin_cannot_revoke_an_owner_token(settings):
    """Minting for an owner was guarded; revoking an owner's token was not.

    Revocation is the more damaging half — it does not grant the delegated admin
    anything, it takes the owner's access to their own instance away. `create_token`
    calls `_protect_owner_target` with an explicit comment about escalation;
    `revoke_token` only checked the capability and then revoked by id.
    """
    app = create_app(settings)
    with TestClient(app) as client:
        storage = app.state.storage
        _issue(storage, "adm2", "admin", "jrc_admin_revoker")
        admin = {"Authorization": "Bearer jrc_admin_revoker"}

        storage.ensure_user(LEGACY_OWNER_USER_ID, preset_key="owner")
        storage.update_user(LEGACY_OWNER_USER_ID, preset_key="owner")
        owner_token = storage.create_api_token(
            LEGACY_OWNER_USER_ID,
            hashlib.sha256(b"jrc_owner_secret").hexdigest(),
            label="owner phone",
            created_by="test",
        )
        refused = client.delete(f"/api/admin/tokens/{owner_token['id']}", headers=admin)
        assert refused.status_code == 403
        assert storage.get_api_token(owner_token["id"])["revoked_at"] is None

        # A normal account's token is still revocable by that same admin.
        ordinary = _issue(storage, "user10", "user", "jrc_ordinary")
        assert client.delete(f"/api/admin/tokens/{ordinary['id']}", headers=admin).status_code == 200
        assert storage.get_api_token(ordinary["id"])["revoked_at"] is not None

        # And the owner may still revoke their own.
        owner = {"Authorization": f"Bearer {settings.api_token}"}
        assert client.delete(f"/api/admin/tokens/{owner_token['id']}", headers=owner).status_code == 200


# --- TTL / expiry ---------------------------------------------------------


def _backdate(storage, token_id: str) -> None:
    past = (datetime.now(UTC) - timedelta(seconds=1)).isoformat(timespec="seconds")
    with storage.transaction() as conn:
        conn.execute("UPDATE api_tokens SET expires_at=? WHERE id=?", (past, token_id))


def test_ttl_token_is_rejected_after_expiry_but_perpetual_survives(storage):
    storage.ensure_user("u1", preset_key="user")
    hashed = hashlib.sha256(b"jrc_ttl").hexdigest()
    record = storage.create_api_token("u1", hashed, label="temp", created_by="t", ttl_seconds=3600)
    assert record["expires_at"] is not None
    assert storage.find_api_token(hashed) is not None  # not yet expired

    _backdate(storage, record["id"])
    assert storage.find_api_token(hashed) is None  # expired -> not found

    # A token minted without a TTL is non-expiring and unaffected.
    perpetual = hashlib.sha256(b"jrc_perm").hexdigest()
    kept = storage.create_api_token("u1", perpetual, created_by="t")
    assert kept["expires_at"] is None
    assert storage.find_api_token(perpetual) is not None
    # Listing exposes expires_at (metadata only, never the hash).
    listed = {row["id"]: row for row in storage.list_api_tokens("u1")}
    assert "expires_at" in listed[record["id"]]
    assert listed[kept["id"]]["expires_at"] is None


def test_create_api_token_rejects_out_of_range_ttl(storage):
    from friday.storage import MAX_API_TOKEN_TTL_SECONDS

    storage.ensure_user("u1", preset_key="user")
    for bad in (0, -5, MAX_API_TOKEN_TTL_SECONDS + 1, 10**30):
        with pytest.raises(ValueError):
            storage.create_api_token("u1", hashlib.sha256(str(bad).encode()).hexdigest(), ttl_seconds=bad)
    # The maximum itself is accepted (no overflow computing expires_at).
    ok = storage.create_api_token(
        "u1", hashlib.sha256(b"maxttl").hexdigest(), ttl_seconds=MAX_API_TOKEN_TTL_SECONDS
    )
    assert ok["expires_at"] is not None


def test_legacy_db_without_expires_at_migrates_and_keeps_tokens_perpetual(
    settings, tmp_path, simulate_legacy_schema
):
    database = tmp_path / "legacy-tokens.sqlite3"
    seed = FridayStorage(replace(settings, database_path=database))
    seed.ensure_user("leg", preset_key="user")
    hashed = hashlib.sha256(b"jrc_legacy").hexdigest()
    record = seed.create_api_token("leg", hashed, label="old", created_by="t")
    seed.close()

    # Simulate a pre-expires_at database (schema 14): drop the column and marker.
    raw = sqlite3.connect(database)
    raw.execute("ALTER TABLE api_tokens DROP COLUMN expires_at")
    simulate_legacy_schema(raw, 14)
    raw.commit()
    raw.close()

    migrated = FridayStorage(replace(settings, database_path=database))
    try:
        columns = {row[1] for row in migrated.execute("PRAGMA table_info(api_tokens)").fetchall()}
        assert "expires_at" in columns  # migration re-added the column
        version = int(
            migrated.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()[0]
        )
        assert version == SCHEMA_VERSION
        found = migrated.find_api_token(hashed)
        assert found is not None and found["id"] == record["id"]
        assert found["expires_at"] is None  # legacy tokens are non-expiring
    finally:
        migrated.close()


def test_expired_token_is_rejected_by_auth(settings):
    app = create_app(settings)
    with TestClient(app) as client:
        record = _issue(app.state.storage, "modx", "moderator", "jrc_expiring")
        header = {"Authorization": "Bearer jrc_expiring"}
        assert client.get("/api/me", headers=header).status_code == 200

        _backdate(app.state.storage, record["id"])
        # The committed expiry is visible to the request handler's own connection.
        assert client.get("/api/me", headers=header).status_code == 401


def test_admin_mint_with_ttl_sets_expiry_and_rejects_bad_ttl(settings):
    app = create_app(settings)
    with TestClient(app) as client:
        owner = {"Authorization": f"Bearer {settings.api_token}"}
        storage = app.state.storage
        storage.ensure_user("modt", source="admin", preset_key="moderator")
        storage.update_user("modt", preset_key="moderator")

        timed = client.post("/api/admin/tokens", json={"user_id": "modt", "ttl_seconds": 3600}, headers=owner)
        assert timed.status_code == 200
        assert timed.json()["expires_at"] is not None

        perpetual = client.post("/api/admin/tokens", json={"user_id": "modt"}, headers=owner)
        assert perpetual.status_code == 200
        assert perpetual.json()["expires_at"] is None

        # Non-positive, non-integer, JSON bool, and huge values are all 400 (never 500).
        for bad in (0, -1, "abc", True, False, 10**30, 1e30):
            rejected = client.post(
                "/api/admin/tokens", json={"user_id": "modt", "ttl_seconds": bad}, headers=owner
            )
            assert rejected.status_code == 400, (
                f"ttl_seconds={bad!r} should be 400, got {rejected.status_code}"
            )


def test_parse_ttl_seconds_units_and_errors():
    assert _parse_ttl_seconds("3600") == 3600
    assert _parse_ttl_seconds("3600s") == 3600
    assert _parse_ttl_seconds("30m") == 1800
    assert _parse_ttl_seconds("24h") == 86400
    assert _parse_ttl_seconds("90d") == 90 * 86400
    # Rejects: empty, non-numeric, unknown unit, non-positive, embedded space/underscore,
    # unicode digits, and absurdly large values (no OverflowError leaks).
    for bad in ("", "abc", "10x", "0", "-5", "-1d", "90 d", "1_000", "１０", "d", "999999999999d"):
        with pytest.raises(ValueError):
            _parse_ttl_seconds(bad)
