"""Durable state for the first-party Obsidian organ.

The note bodies live in the per-user vault checkout. SQLite stores only identity,
onboarding, operation and delivery facts; API keys remain in the private
Syncthing profile and are referenced, never copied into the database.
"""

from __future__ import annotations

import json
import sqlite3
import unicodedata
from collections.abc import Mapping, Sequence
from functools import lru_cache
from typing import Any

from friday.storage._base import StorageShared, validate_user_id
from friday.storage.models import new_id, utc_now

OBSIDIAN_SCHEMA = """
CREATE TABLE IF NOT EXISTS obsidian_sync_profiles (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    config_root TEXT NOT NULL,
    database_root TEXT NOT NULL,
    api_endpoint TEXT NOT NULL,
    api_key_ref TEXT NOT NULL,
    server_device_id TEXT NOT NULL DEFAULT '',
    syncthing_version TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL DEFAULT 'provisioning'
        CHECK(state IN ('provisioning', 'running', 'stopped', 'failed', 'disconnected')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_obsidian_profile_user
    ON obsidian_sync_profiles(user_id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_obsidian_profile_id_user
    ON obsidian_sync_profiles(id, user_id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_obsidian_profile_config_root
    ON obsidian_sync_profiles(config_root);
CREATE UNIQUE INDEX IF NOT EXISTS uq_obsidian_profile_database_root
    ON obsidian_sync_profiles(database_root);
CREATE UNIQUE INDEX IF NOT EXISTS uq_obsidian_profile_api_endpoint
    ON obsidian_sync_profiles(api_endpoint);
CREATE UNIQUE INDEX IF NOT EXISTS uq_obsidian_profile_device_id
    ON obsidian_sync_profiles(server_device_id) WHERE server_device_id<>'';

CREATE TABLE IF NOT EXISTS obsidian_android_devices (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    profile_id TEXT NOT NULL,
    syncthing_device_id TEXT NOT NULL,
    display_name TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL DEFAULT 'detected'
        CHECK(state IN ('detected', 'connected', 'offline', 'paused', 'disconnected')),
    last_seen_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(profile_id, user_id)
        REFERENCES obsidian_sync_profiles(id, user_id) ON DELETE CASCADE
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_obsidian_android_profile
    ON obsidian_android_devices(profile_id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_obsidian_android_id_user
    ON obsidian_android_devices(id, user_id);
-- First-release policy: one physical Syncthing identity belongs to one Friday
-- account. Multi-account phones require an explicit product decision later.
CREATE UNIQUE INDEX IF NOT EXISTS uq_obsidian_android_device_id
    ON obsidian_android_devices(syncthing_device_id);
CREATE INDEX IF NOT EXISTS idx_obsidian_android_user
    ON obsidian_android_devices(user_id);

CREATE TABLE IF NOT EXISTS obsidian_vaults (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    profile_id TEXT NOT NULL,
    android_device_id TEXT REFERENCES obsidian_android_devices(id) ON DELETE SET NULL,
    display_name TEXT NOT NULL DEFAULT 'Friday',
    folder_id TEXT NOT NULL,
    server_path TEXT NOT NULL,
    android_vault_name TEXT NOT NULL DEFAULT 'Friday',
    android_path_hint TEXT NOT NULL DEFAULT 'Documents/Obsidian/Friday',
    state TEXT NOT NULL DEFAULT 'provisioning'
        CHECK(state IN (
            'provisioning', 'offering_folder', 'awaiting_folder_acceptance',
            'initial_sync', 'awaiting_vault_registration', 'verifying',
            'ready', 'disconnected', 'failed'
        )),
    convention_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK(json_valid(convention_json) AND json_type(convention_json)='object'),
    FOREIGN KEY(profile_id, user_id)
        REFERENCES obsidian_sync_profiles(id, user_id) ON DELETE CASCADE
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_obsidian_vault_user
    ON obsidian_vaults(user_id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_obsidian_vault_id_user
    ON obsidian_vaults(id, user_id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_obsidian_vault_profile
    ON obsidian_vaults(profile_id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_obsidian_vault_folder
    ON obsidian_vaults(folder_id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_obsidian_vault_server_path
    ON obsidian_vaults(server_path);
CREATE TRIGGER IF NOT EXISTS obsidian_vault_device_owner_insert
BEFORE INSERT ON obsidian_vaults
WHEN NEW.android_device_id IS NOT NULL
 AND NOT EXISTS(
     SELECT 1 FROM obsidian_android_devices
      WHERE id=NEW.android_device_id AND user_id=NEW.user_id
 )
BEGIN
    SELECT RAISE(ABORT, 'obsidian vault/device owner mismatch');
END;
CREATE TRIGGER IF NOT EXISTS obsidian_vault_device_owner_update
BEFORE UPDATE OF android_device_id, user_id ON obsidian_vaults
WHEN NEW.android_device_id IS NOT NULL
 AND NOT EXISTS(
     SELECT 1 FROM obsidian_android_devices
      WHERE id=NEW.android_device_id AND user_id=NEW.user_id
 )
BEGIN
    SELECT RAISE(ABORT, 'obsidian vault/device owner mismatch');
END;

CREATE TABLE IF NOT EXISTS obsidian_onboarding_sessions (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    profile_id TEXT NOT NULL,
    vault_id TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'provisioning_server_profile'
        CHECK(state IN (
            'not_connected', 'provisioning_server_profile',
            'awaiting_device_id_handoff', 'awaiting_android_device',
            'android_device_detected', 'multiple_pending_devices',
            'offering_folder', 'awaiting_android_folder_acceptance',
            'initial_sync', 'awaiting_obsidian_vault_registration',
            'round_trip_verification', 'ready', 'cancelled',
            'disconnected', 'failed'
        )),
    setup_token_hash TEXT NOT NULL,
    setup_token_used_at TEXT,
    telegram_chat_id TEXT NOT NULL DEFAULT '',
    device_id_presented_at TEXT,
    obsidian_opened_at TEXT,
    pending_device_id TEXT,
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK(length(setup_token_hash)=64 AND setup_token_hash NOT GLOB '*[^0-9a-f]*'),
    FOREIGN KEY(profile_id, user_id)
        REFERENCES obsidian_sync_profiles(id, user_id) ON DELETE CASCADE,
    FOREIGN KEY(vault_id, user_id)
        REFERENCES obsidian_vaults(id, user_id) ON DELETE CASCADE
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_obsidian_onboarding_user
    ON obsidian_onboarding_sessions(user_id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_obsidian_onboarding_profile
    ON obsidian_onboarding_sessions(profile_id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_obsidian_onboarding_vault
    ON obsidian_onboarding_sessions(vault_id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_obsidian_onboarding_token
    ON obsidian_onboarding_sessions(setup_token_hash);
CREATE UNIQUE INDEX IF NOT EXISTS uq_obsidian_onboarding_id_user
    ON obsidian_onboarding_sessions(id, user_id);

CREATE TABLE IF NOT EXISTS obsidian_pairing_candidates (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    session_id TEXT NOT NULL,
    syncthing_device_id TEXT NOT NULL,
    display_name TEXT NOT NULL DEFAULT '',
    short_suffix TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending'
        CHECK(state IN ('pending', 'selected', 'rejected', 'expired')),
    detected_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(session_id, user_id)
        REFERENCES obsidian_onboarding_sessions(id, user_id) ON DELETE CASCADE
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_obsidian_candidate_session_device
    ON obsidian_pairing_candidates(session_id, syncthing_device_id);
CREATE INDEX IF NOT EXISTS idx_obsidian_candidate_owner_state
    ON obsidian_pairing_candidates(user_id, state, detected_at DESC);

CREATE TABLE IF NOT EXISTS obsidian_operations (
    id TEXT NOT NULL,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    work_item_id TEXT,
    vault_id TEXT NOT NULL,
    method TEXT NOT NULL CHECK(method IN (
        'create', 'append', 'set_properties', 'daily_note', 'verification_note'
    )),
    arguments_digest TEXT NOT NULL,
    expected_revision TEXT,
    status TEXT NOT NULL DEFAULT 'prepared'
        CHECK(status IN (
            'prepared', 'committed', 'scan_pending', 'scan_complete',
            'delivery_pending', 'delivered', 'conflict', 'failed',
            'uncertain', 'reconciled', 'cancelled'
        )),
    result_json TEXT NOT NULL DEFAULT '{}',
    delivery_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(user_id, id),
    CHECK(length(arguments_digest)=64 AND arguments_digest NOT GLOB '*[^0-9a-f]*'),
    CHECK(json_valid(result_json) AND json_type(result_json)='object'),
    CHECK(json_valid(delivery_json) AND json_type(delivery_json)='object'),
    FOREIGN KEY(vault_id, user_id)
        REFERENCES obsidian_vaults(id, user_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_obsidian_operations_user_time
    ON obsidian_operations(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_obsidian_operations_delivery
    ON obsidian_operations(status, updated_at)
    WHERE status IN ('committed', 'scan_pending', 'scan_complete', 'delivery_pending', 'uncertain');

CREATE TABLE IF NOT EXISTS obsidian_conflicts (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    vault_id TEXT NOT NULL,
    canonical_path TEXT NOT NULL,
    conflict_path TEXT NOT NULL,
    detected_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open'
        CHECK(status IN ('open', 'resolved', 'dismissed')),
    resolution_json TEXT,
    updated_at TEXT NOT NULL,
    CHECK(resolution_json IS NULL OR (
        json_valid(resolution_json) AND json_type(resolution_json)='object'
    )),
    FOREIGN KEY(vault_id, user_id)
        REFERENCES obsidian_vaults(id, user_id) ON DELETE CASCADE
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_obsidian_conflict_path
    ON obsidian_conflicts(vault_id, conflict_path);
CREATE INDEX IF NOT EXISTS idx_obsidian_conflicts_user_status
    ON obsidian_conflicts(user_id, status, detected_at DESC);
"""

_ONBOARDING_TRANSITIONS: dict[str, frozenset[str]] = {
    "not_connected": frozenset({"provisioning_server_profile", "cancelled"}),
    "provisioning_server_profile": frozenset(
        {"awaiting_device_id_handoff", "android_device_detected", "failed", "cancelled"}
    ),
    "awaiting_device_id_handoff": frozenset(
        {"awaiting_android_device", "android_device_detected", "failed", "cancelled"}
    ),
    "awaiting_android_device": frozenset(
        {"android_device_detected", "multiple_pending_devices", "failed", "cancelled"}
    ),
    "multiple_pending_devices": frozenset({"android_device_detected", "failed", "cancelled"}),
    "android_device_detected": frozenset({"offering_folder", "failed", "cancelled"}),
    "offering_folder": frozenset({"awaiting_android_folder_acceptance", "failed", "cancelled"}),
    "awaiting_android_folder_acceptance": frozenset({"initial_sync", "failed", "cancelled"}),
    "initial_sync": frozenset({"awaiting_obsidian_vault_registration", "failed", "cancelled"}),
    "awaiting_obsidian_vault_registration": frozenset({"round_trip_verification", "failed", "cancelled"}),
    "round_trip_verification": frozenset({"ready", "failed", "cancelled"}),
    "ready": frozenset({"disconnected"}),
    "failed": frozenset({"provisioning_server_profile", "disconnected", "cancelled"}),
    "disconnected": frozenset({"provisioning_server_profile", "cancelled"}),
    "cancelled": frozenset({"provisioning_server_profile"}),
}

_OPERATION_TRANSITIONS: dict[str, frozenset[str]] = {
    "prepared": frozenset({"committed", "conflict", "failed", "uncertain", "cancelled"}),
    "committed": frozenset({"scan_pending", "scan_complete", "uncertain", "failed"}),
    "scan_pending": frozenset({"scan_complete", "delivery_pending", "uncertain", "failed"}),
    "scan_complete": frozenset({"delivery_pending", "delivered", "uncertain", "failed"}),
    "delivery_pending": frozenset({"delivered", "conflict", "uncertain", "failed"}),
    "uncertain": frozenset({"reconciled", "conflict", "failed"}),
    "reconciled": frozenset({"scan_pending", "scan_complete", "delivery_pending", "delivered"}),
    "delivered": frozenset(),
    "conflict": frozenset({"reconciled", "cancelled"}),
    "failed": frozenset({"reconciled", "cancelled"}),
    "cancelled": frozenset(),
}


def _canonical_json(value: Mapping[str, Any] | None) -> str:
    return json.dumps(dict(value or {}), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


@lru_cache(maxsize=1)
def _canonical_schema_objects() -> dict[tuple[str, str], str]:
    conn = sqlite3.connect(":memory:")
    try:
        conn.executescript(OBSIDIAN_SCHEMA)
        return {
            (str(item[0]), str(item[1])): str(item[2])
            for item in conn.execute(
                """SELECT type, name, sql FROM sqlite_master
                   WHERE sql IS NOT NULL AND tbl_name LIKE 'obsidian_%'
                   ORDER BY type, name"""
            )
        }
    finally:
        conn.close()


def validate_obsidian_schema(conn: sqlite3.Connection) -> None:
    """Reject a current marker whose authoritative Obsidian schema was altered."""

    expected = _canonical_schema_objects()
    installed = {
        (str(item[0]), str(item[1])): str(item[2])
        for item in conn.execute(
            """SELECT type, name, sql FROM sqlite_master
               WHERE sql IS NOT NULL AND tbl_name LIKE 'obsidian_%'
               ORDER BY type, name"""
        )
    }
    missing = expected.keys() - installed.keys()
    altered = {key for key in expected.keys() & installed.keys() if expected[key] != installed[key]}
    unexpected = installed.keys() - expected.keys()
    if missing or altered or unexpected:
        raise sqlite3.DatabaseError(
            "Schema 35 Obsidian state is incomplete or altered "
            f"(missing={len(missing)}, altered={len(altered)}, unexpected={len(unexpected)})"
        )


class ObsidianMixin(StorageShared):
    """Tenant-scoped persistence used by the Obsidian service and workers."""

    def get_obsidian_profile(self, user_id: str) -> dict[str, Any] | None:
        owner = validate_user_id(user_id)
        return _row(self.execute("SELECT * FROM obsidian_sync_profiles WHERE user_id=?", (owner,)).fetchone())

    def list_obsidian_profiles(self, *, limit: int = 64) -> list[dict[str, Any]]:
        """Return the bounded supervisor inventory for internal reconciliation."""

        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 512:
            raise ValueError("Obsidian profile limit must be between 1 and 512")
        rows = self.execute(
            "SELECT * FROM obsidian_sync_profiles ORDER BY created_at, id LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]

    def get_obsidian_vault(self, user_id: str) -> dict[str, Any] | None:
        owner = validate_user_id(user_id)
        return _row(self.execute("SELECT * FROM obsidian_vaults WHERE user_id=?", (owner,)).fetchone())

    def get_obsidian_device(self, user_id: str) -> dict[str, Any] | None:
        owner = validate_user_id(user_id)
        return _row(
            self.execute("SELECT * FROM obsidian_android_devices WHERE user_id=?", (owner,)).fetchone()
        )

    def get_obsidian_onboarding(self, user_id: str) -> dict[str, Any] | None:
        owner = validate_user_id(user_id)
        return _row(
            self.execute("SELECT * FROM obsidian_onboarding_sessions WHERE user_id=?", (owner,)).fetchone()
        )

    def create_obsidian_bundle(
        self,
        user_id: str,
        *,
        config_root: str,
        database_root: str,
        api_endpoint: str,
        api_key_ref: str,
        server_path: str,
        folder_id: str,
        setup_token_hash: str,
        expires_at: str,
        telegram_chat_id: str = "",
        display_name: str = "Friday",
        android_vault_name: str = "Friday",
        android_path_hint: str = "Documents/Obsidian/Friday",
        convention: Mapping[str, Any] | None = None,
        max_profiles: int = 64,
    ) -> dict[str, dict[str, Any]]:
        """Create the one-user/one-profile/one-vault aggregate exactly once."""

        owner = validate_user_id(user_id)
        digest = str(setup_token_hash or "").strip().casefold()
        if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
            raise ValueError("setup_token_hash must be a lowercase SHA-256 digest")
        if not all(
            str(value or "").strip()
            for value in (
                config_root,
                database_root,
                api_endpoint,
                api_key_ref,
                server_path,
                folder_id,
                expires_at,
            )
        ):
            raise ValueError("Obsidian profile, vault and session fields must be non-empty")
        if (
            isinstance(max_profiles, bool)
            or not isinstance(max_profiles, int)
            or not 1 <= max_profiles <= 512
        ):
            raise ValueError("Obsidian profile limit must be between 1 and 512")
        now = utc_now()
        with self.transaction() as conn:
            user = conn.execute("SELECT status FROM users WHERE id=?", (owner,)).fetchone()
            if user is None or str(user[0]) != "active":
                raise ValueError("Obsidian onboarding requires an active Friday user")
            profile = conn.execute(
                "SELECT * FROM obsidian_sync_profiles WHERE user_id=?", (owner,)
            ).fetchone()
            vault = conn.execute("SELECT * FROM obsidian_vaults WHERE user_id=?", (owner,)).fetchone()
            session = conn.execute(
                "SELECT * FROM obsidian_onboarding_sessions WHERE user_id=?", (owner,)
            ).fetchone()
            present = tuple(item is not None for item in (profile, vault, session))
            if any(present) and not all(present):
                raise sqlite3.IntegrityError("Obsidian aggregate is incomplete")
            if all(present):
                assert profile is not None and vault is not None and session is not None
                return {"profile": dict(profile), "vault": dict(vault), "session": dict(session)}
            profile_count = int(conn.execute("SELECT COUNT(*) FROM obsidian_sync_profiles").fetchone()[0])
            if profile_count >= max_profiles:
                raise ValueError("Obsidian profile limit reached")

            profile_id = new_id("stprof")
            vault_id = new_id("obsvault")
            session_id = new_id("obssetup")
            conn.execute(
                """INSERT INTO obsidian_sync_profiles(
                       id, user_id, config_root, database_root, api_endpoint,
                       api_key_ref, state, created_at, updated_at
                   ) VALUES(?, ?, ?, ?, ?, ?, 'provisioning', ?, ?)""",
                (
                    profile_id,
                    owner,
                    str(config_root),
                    str(database_root),
                    str(api_endpoint),
                    str(api_key_ref),
                    now,
                    now,
                ),
            )
            conn.execute(
                """INSERT INTO obsidian_vaults(
                       id, user_id, profile_id, display_name, folder_id, server_path,
                       android_vault_name, android_path_hint, state, convention_json,
                       created_at, updated_at
                   ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, 'provisioning', ?, ?, ?)""",
                (
                    vault_id,
                    owner,
                    profile_id,
                    str(display_name),
                    str(folder_id),
                    str(server_path),
                    str(android_vault_name),
                    str(android_path_hint),
                    _canonical_json(convention),
                    now,
                    now,
                ),
            )
            conn.execute(
                """INSERT INTO obsidian_onboarding_sessions(
                       id, user_id, profile_id, vault_id, state, setup_token_hash,
                       telegram_chat_id, expires_at, created_at, updated_at
                   ) VALUES(?, ?, ?, ?, 'provisioning_server_profile', ?, ?, ?, ?, ?)""",
                (
                    session_id,
                    owner,
                    profile_id,
                    vault_id,
                    digest,
                    str(telegram_chat_id),
                    str(expires_at),
                    now,
                    now,
                ),
            )
            profile = conn.execute(
                "SELECT * FROM obsidian_sync_profiles WHERE id=?", (profile_id,)
            ).fetchone()
            vault = conn.execute("SELECT * FROM obsidian_vaults WHERE id=?", (vault_id,)).fetchone()
            session = conn.execute(
                "SELECT * FROM obsidian_onboarding_sessions WHERE id=?", (session_id,)
            ).fetchone()
            assert profile is not None and vault is not None and session is not None
            return {"profile": dict(profile), "vault": dict(vault), "session": dict(session)}

    def update_obsidian_profile(
        self,
        user_id: str,
        *,
        state: str,
        server_device_id: str | None = None,
        syncthing_version: str | None = None,
    ) -> dict[str, Any]:
        owner = validate_user_id(user_id)
        if state not in {"provisioning", "running", "stopped", "failed", "disconnected"}:
            raise ValueError("invalid Obsidian profile state")
        fields = ["state=?", "updated_at=?"]
        values: list[Any] = [state, utc_now()]
        if server_device_id is not None:
            fields.append("server_device_id=?")
            values.append(str(server_device_id))
        if syncthing_version is not None:
            fields.append("syncthing_version=?")
            values.append(str(syncthing_version))
        values.append(owner)
        with self.transaction() as conn:
            cursor = conn.execute(
                f"UPDATE obsidian_sync_profiles SET {', '.join(fields)} WHERE user_id=?",  # nosec B608
                tuple(values),
            )
            if cursor.rowcount != 1:
                raise ValueError("Obsidian profile not found")
            row = conn.execute("SELECT * FROM obsidian_sync_profiles WHERE user_id=?", (owner,)).fetchone()
            assert row is not None
            return dict(row)

    def update_obsidian_vault(self, user_id: str, *, state: str) -> dict[str, Any]:
        owner = validate_user_id(user_id)
        valid = {
            "provisioning",
            "offering_folder",
            "awaiting_folder_acceptance",
            "initial_sync",
            "awaiting_vault_registration",
            "verifying",
            "ready",
            "disconnected",
            "failed",
        }
        if state not in valid:
            raise ValueError("invalid Obsidian vault state")
        with self.transaction() as conn:
            cursor = conn.execute(
                "UPDATE obsidian_vaults SET state=?, updated_at=? WHERE user_id=?",
                (state, utc_now(), owner),
            )
            if cursor.rowcount != 1:
                raise ValueError("Obsidian vault not found")
            row = conn.execute("SELECT * FROM obsidian_vaults WHERE user_id=?", (owner,)).fetchone()
            assert row is not None
            return dict(row)

    def update_obsidian_vault_alias(self, user_id: str, alias: str) -> dict[str, Any]:
        """Persist the owner-scoped Android Obsidian vault name used in URIs."""

        owner = validate_user_id(user_id)
        if not isinstance(alias, str):
            raise TypeError("Obsidian vault alias must be text")
        normalized = unicodedata.normalize("NFC", alias).strip()
        if not 1 <= len(normalized) <= 100:
            raise ValueError("Obsidian vault alias must contain between 1 and 100 characters")
        if len(normalized.encode("utf-8", errors="strict")) > 256:
            raise ValueError("Obsidian vault alias is too large")
        if (
            "/" in normalized
            or "\\" in normalized
            or any(unicodedata.category(character).startswith("C") for character in normalized)
        ):
            raise ValueError("Obsidian vault alias contains an unsafe character")
        with self.transaction() as conn:
            cursor = conn.execute(
                "UPDATE obsidian_vaults SET android_vault_name=?, updated_at=? WHERE user_id=?",
                (normalized, utc_now(), owner),
            )
            if cursor.rowcount != 1:
                raise ValueError("Obsidian vault not found")
            row = conn.execute("SELECT * FROM obsidian_vaults WHERE user_id=?", (owner,)).fetchone()
            assert row is not None
            return dict(row)

    def update_obsidian_device(
        self,
        user_id: str,
        *,
        state: str,
        seen: bool = False,
    ) -> dict[str, Any]:
        owner = validate_user_id(user_id)
        if state not in {"detected", "connected", "offline", "paused", "disconnected"}:
            raise ValueError("invalid Obsidian Android device state")
        now = utc_now()
        with self.transaction() as conn:
            cursor = conn.execute(
                """UPDATE obsidian_android_devices
                   SET state=?, last_seen_at=CASE WHEN ? THEN ? ELSE last_seen_at END,
                       updated_at=? WHERE user_id=?""",
                (state, int(seen), now, now, owner),
            )
            if cursor.rowcount != 1:
                raise ValueError("Obsidian Android device not found")
            row = conn.execute("SELECT * FROM obsidian_android_devices WHERE user_id=?", (owner,)).fetchone()
            assert row is not None
            return dict(row)

    def transition_obsidian_onboarding(
        self,
        user_id: str,
        state: str,
        *,
        pending_device_id: str | None = None,
        device_id_presented: bool = False,
        obsidian_opened: bool = False,
    ) -> dict[str, Any]:
        owner = validate_user_id(user_id)
        with self.transaction() as conn:
            current = conn.execute(
                "SELECT * FROM obsidian_onboarding_sessions WHERE user_id=?", (owner,)
            ).fetchone()
            if current is None:
                raise ValueError("Obsidian onboarding session not found")
            old_state = str(current["state"])
            if state != old_state and state not in _ONBOARDING_TRANSITIONS.get(old_state, frozenset()):
                raise ValueError(f"invalid Obsidian onboarding transition: {old_state} -> {state}")
            now = utc_now()
            presented_at = current["device_id_presented_at"]
            if device_id_presented and presented_at is None:
                presented_at = now
            opened_at = current["obsidian_opened_at"]
            if obsidian_opened and opened_at is None:
                opened_at = now
            device_id = current["pending_device_id"] if pending_device_id is None else pending_device_id
            conn.execute(
                """UPDATE obsidian_onboarding_sessions
                   SET state=?, pending_device_id=?, device_id_presented_at=?,
                       obsidian_opened_at=?, updated_at=?
                   WHERE id=?""",
                (state, device_id, presented_at, opened_at, now, current["id"]),
            )
            updated = conn.execute(
                "SELECT * FROM obsidian_onboarding_sessions WHERE id=?", (current["id"],)
            ).fetchone()
            assert updated is not None
            return dict(updated)

    def finalize_obsidian_onboarding(self, user_id: str) -> dict[str, Any]:
        """Publish session and vault readiness in one owner-scoped transaction."""

        owner = validate_user_id(user_id)
        with self.transaction() as conn:
            session = conn.execute(
                "SELECT * FROM obsidian_onboarding_sessions WHERE user_id=?", (owner,)
            ).fetchone()
            vault = conn.execute("SELECT * FROM obsidian_vaults WHERE user_id=?", (owner,)).fetchone()
            if session is None or vault is None:
                raise ValueError("Obsidian onboarding aggregate not found")
            state = str(session["state"])
            if state not in {"round_trip_verification", "ready"}:
                raise ValueError("Obsidian onboarding is not ready to finalize")
            if session["obsidian_opened_at"] is None:
                raise ValueError("Obsidian verification has not been confirmed")
            now = utc_now()
            conn.execute(
                "UPDATE obsidian_onboarding_sessions SET state='ready', updated_at=? WHERE id=?",
                (now, session["id"]),
            )
            conn.execute(
                "UPDATE obsidian_vaults SET state='ready', updated_at=? WHERE id=? AND user_id=?",
                (now, vault["id"], owner),
            )
            updated = conn.execute(
                "SELECT * FROM obsidian_onboarding_sessions WHERE id=?", (session["id"],)
            ).fetchone()
            assert updated is not None
            return dict(updated)

    def rotate_obsidian_setup_token(
        self,
        user_id: str,
        *,
        setup_token_hash: str,
        expires_at: str,
    ) -> dict[str, Any]:
        owner = validate_user_id(user_id)
        digest = str(setup_token_hash or "").strip().casefold()
        if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
            raise ValueError("setup_token_hash must be a lowercase SHA-256 digest")
        if not str(expires_at or "").strip():
            raise ValueError("expires_at is required")
        with self.transaction() as conn:
            cursor = conn.execute(
                """UPDATE obsidian_onboarding_sessions
                   SET setup_token_hash=?, setup_token_used_at=NULL, expires_at=?, updated_at=?
                   WHERE user_id=?""",
                (digest, str(expires_at), utc_now(), owner),
            )
            if cursor.rowcount != 1:
                raise ValueError("Obsidian onboarding session not found")
            row = conn.execute(
                "SELECT * FROM obsidian_onboarding_sessions WHERE user_id=?", (owner,)
            ).fetchone()
            assert row is not None
            return dict(row)

    def consume_obsidian_setup_token(
        self,
        setup_token_hash: str,
        *,
        now: str | None = None,
    ) -> dict[str, Any] | None:
        """Resolve one public setup token exactly once without exposing its owner."""

        digest = str(setup_token_hash or "").strip().casefold()
        if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
            return None
        observed_at = str(now or utc_now())
        with self.transaction() as conn:
            row = conn.execute(
                """SELECT s.id AS session_id, s.state, s.expires_at,
                          p.server_device_id, v.display_name, v.android_path_hint
                     FROM obsidian_onboarding_sessions s
                     JOIN obsidian_sync_profiles p
                       ON p.id=s.profile_id AND p.user_id=s.user_id
                     JOIN obsidian_vaults v
                       ON v.id=s.vault_id AND v.user_id=s.user_id
                    WHERE s.setup_token_hash=? AND s.setup_token_used_at IS NULL
                      AND s.state NOT IN ('cancelled', 'failed', 'disconnected')
                      AND s.expires_at>?""",
                (digest, observed_at),
            ).fetchone()
            if row is None:
                return None
            cursor = conn.execute(
                """UPDATE obsidian_onboarding_sessions
                   SET setup_token_used_at=?, updated_at=?
                   WHERE id=? AND setup_token_hash=? AND setup_token_used_at IS NULL""",
                (observed_at, observed_at, row["session_id"], digest),
            )
            if cursor.rowcount != 1:
                return None
            return dict(row)

    def bind_obsidian_android_device(
        self,
        user_id: str,
        *,
        syncthing_device_id: str,
        display_name: str = "",
    ) -> dict[str, Any]:
        owner = validate_user_id(user_id)
        device_id = str(syncthing_device_id or "").strip().upper()
        if not device_id:
            raise ValueError("syncthing_device_id is required")
        now = utc_now()
        with self.transaction() as conn:
            profile = conn.execute(
                "SELECT id FROM obsidian_sync_profiles WHERE user_id=?", (owner,)
            ).fetchone()
            if profile is None:
                raise ValueError("Obsidian profile not found")
            existing = conn.execute(
                "SELECT * FROM obsidian_android_devices WHERE profile_id=?", (profile["id"],)
            ).fetchone()
            if existing is not None:
                if str(existing["syncthing_device_id"]) != device_id:
                    raise ValueError("A different Android device is already bound to this profile")
                return dict(existing)
            record_id = new_id("stdev")
            conn.execute(
                """INSERT INTO obsidian_android_devices(
                       id, user_id, profile_id, syncthing_device_id, display_name,
                       state, last_seen_at, created_at, updated_at
                   ) VALUES(?, ?, ?, ?, ?, 'detected', ?, ?, ?)""",
                (record_id, owner, profile["id"], device_id, str(display_name)[:200], now, now, now),
            )
            conn.execute(
                "UPDATE obsidian_vaults SET android_device_id=?, updated_at=? WHERE user_id=?",
                (record_id, now, owner),
            )
            row = conn.execute("SELECT * FROM obsidian_android_devices WHERE id=?", (record_id,)).fetchone()
            assert row is not None
            return dict(row)

    def record_obsidian_pairing_candidates(
        self,
        user_id: str,
        candidates: Sequence[Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        """Persist one bounded pending-device snapshot without exposing raw IDs as callbacks."""

        owner = validate_user_id(user_id)
        if len(candidates) > 8:
            raise ValueError("At most eight Obsidian pairing candidates are accepted")
        normalized: list[tuple[str, str]] = []
        seen: set[str] = set()
        for candidate in candidates:
            device_id = str(candidate.get("syncthing_device_id") or "").strip().upper()
            display_name = str(candidate.get("display_name") or "").strip()[:200]
            if not device_id or len(device_id) > 128:
                raise ValueError("Pairing candidate has an invalid Syncthing device ID")
            if device_id in seen:
                raise ValueError("Pairing candidate device IDs must be unique")
            seen.add(device_id)
            normalized.append((device_id, display_name))

        now = utc_now()
        with self.transaction() as conn:
            session = conn.execute(
                "SELECT id, state, expires_at FROM obsidian_onboarding_sessions WHERE user_id=?", (owner,)
            ).fetchone()
            if session is None:
                raise ValueError("Obsidian onboarding session not found")
            if str(session["state"]) not in {"awaiting_android_device", "multiple_pending_devices"}:
                raise ValueError("Obsidian onboarding is not discovering a device")
            if str(session["expires_at"]) <= now:
                raise ValueError("Obsidian onboarding session expired")
            existing = conn.execute(
                "SELECT * FROM obsidian_pairing_candidates WHERE user_id=? AND session_id=?",
                (owner, session["id"]),
            ).fetchall()
            by_device = {str(row["syncthing_device_id"]): row for row in existing}
            for row in existing:
                if str(row["syncthing_device_id"]) not in seen and str(row["state"]) == "pending":
                    conn.execute(
                        "UPDATE obsidian_pairing_candidates SET state='expired', updated_at=? WHERE id=?",
                        (now, row["id"]),
                    )
            for device_id, display_name in normalized:
                row = by_device.get(device_id)
                if row is None:
                    conn.execute(
                        """INSERT INTO obsidian_pairing_candidates(
                               id, user_id, session_id, syncthing_device_id, display_name,
                               short_suffix, state, detected_at, expires_at, updated_at
                           ) VALUES(?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)""",
                        (
                            new_id("obscand"),
                            owner,
                            session["id"],
                            device_id,
                            display_name,
                            device_id[-7:],
                            now,
                            session["expires_at"],
                            now,
                        ),
                    )
                else:
                    conn.execute(
                        """UPDATE obsidian_pairing_candidates
                           SET display_name=?, expires_at=?,
                               state=CASE WHEN state='selected' THEN 'selected' ELSE 'pending' END,
                               detected_at=CASE
                                   WHEN state IN ('expired', 'rejected') THEN ? ELSE detected_at END,
                               updated_at=? WHERE id=?""",
                        (display_name, session["expires_at"], now, now, row["id"]),
                    )
            rows = conn.execute(
                """SELECT * FROM obsidian_pairing_candidates
                   WHERE user_id=? AND session_id=? AND state IN ('pending', 'selected')
                   ORDER BY detected_at, id""",
                (owner, session["id"]),
            ).fetchall()
            return [dict(row) for row in rows]

    def list_obsidian_pairing_candidates(self, user_id: str) -> list[dict[str, Any]]:
        owner = validate_user_id(user_id)
        rows = self.execute(
            """SELECT * FROM obsidian_pairing_candidates
               WHERE user_id=? AND state IN ('pending', 'selected')
               ORDER BY detected_at, id""",
            (owner,),
        ).fetchall()
        return [dict(row) for row in rows]

    def select_obsidian_pairing_candidate(self, user_id: str, candidate_id: str) -> dict[str, Any]:
        """Select one owner-scoped opaque candidate and retire every alternative."""

        owner = validate_user_id(user_id)
        opaque_id = str(candidate_id or "").strip()
        with self.transaction() as conn:
            candidate = conn.execute(
                """SELECT c.*, s.state AS session_state
                     FROM obsidian_pairing_candidates c
                     JOIN obsidian_onboarding_sessions s
                       ON s.id=c.session_id AND s.user_id=c.user_id
                    WHERE c.id=? AND c.user_id=?""",
                (opaque_id, owner),
            ).fetchone()
            if candidate is None or str(candidate["state"]) not in {"pending", "selected"}:
                raise ValueError("Obsidian pairing candidate not found")
            if str(candidate["expires_at"]) <= utc_now():
                raise ValueError("Obsidian pairing candidate expired")
            if str(candidate["session_state"]) not in {
                "awaiting_android_device",
                "multiple_pending_devices",
                "android_device_detected",
            }:
                raise ValueError("Obsidian onboarding is not accepting a device")
            now = utc_now()
            conn.execute(
                """UPDATE obsidian_pairing_candidates
                   SET state=CASE WHEN id=? THEN 'selected' ELSE 'rejected' END,
                       updated_at=?
                   WHERE session_id=? AND user_id=? AND state IN ('pending', 'selected')""",
                (opaque_id, now, candidate["session_id"], owner),
            )
            conn.execute(
                """UPDATE obsidian_onboarding_sessions
                   SET pending_device_id=?, updated_at=? WHERE id=? AND user_id=?""",
                (candidate["syncthing_device_id"], now, candidate["session_id"], owner),
            )
            selected = conn.execute(
                "SELECT * FROM obsidian_pairing_candidates WHERE id=? AND user_id=?",
                (opaque_id, owner),
            ).fetchone()
            assert selected is not None
            return dict(selected)

    def prepare_obsidian_operation(
        self,
        user_id: str,
        *,
        operation_id: str,
        vault_id: str,
        method: str,
        arguments_digest: str,
        expected_revision: str | None = None,
        work_item_id: str | None = None,
    ) -> tuple[dict[str, Any], bool]:
        owner = validate_user_id(user_id)
        op_id = str(operation_id or "").strip()
        digest = str(arguments_digest or "").strip().casefold()
        if not op_id or len(op_id) > 200:
            raise ValueError("operation_id is required and must be at most 200 characters")
        if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
            raise ValueError("arguments_digest must be a lowercase SHA-256 digest")
        if work_item_id is not None and (
            not isinstance(work_item_id, str)
            or not work_item_id.strip()
            or len(work_item_id.strip()) > 200
            or "\x00" in work_item_id
        ):
            raise ValueError("work_item_id must be a bounded non-empty string")
        normalized_work_item = work_item_id.strip() if work_item_id is not None else None
        now = utc_now()
        with self.transaction() as conn:
            existing = conn.execute(
                "SELECT * FROM obsidian_operations WHERE id=? AND user_id=?", (op_id, owner)
            ).fetchone()
            if existing is not None:
                identity = (
                    str(existing["user_id"]),
                    str(existing["vault_id"]),
                    str(existing["method"]),
                    str(existing["arguments_digest"]),
                    str(existing["expected_revision"] or ""),
                    str(existing["work_item_id"] or ""),
                )
                requested = (
                    owner,
                    str(vault_id),
                    str(method),
                    digest,
                    str(expected_revision or ""),
                    str(normalized_work_item or ""),
                )
                if identity != requested:
                    raise ValueError("operation_id was already used for different arguments")
                return dict(existing), False
            vault = conn.execute(
                "SELECT id FROM obsidian_vaults WHERE id=? AND user_id=?", (str(vault_id), owner)
            ).fetchone()
            if vault is None:
                raise ValueError("Obsidian vault not found")
            conn.execute(
                """INSERT INTO obsidian_operations(
                       id, user_id, work_item_id, vault_id, method, arguments_digest,
                       expected_revision, status, created_at, updated_at
                   ) VALUES(?, ?, ?, ?, ?, ?, ?, 'prepared', ?, ?)""",
                (
                    op_id,
                    owner,
                    normalized_work_item,
                    str(vault_id),
                    str(method),
                    digest,
                    expected_revision,
                    now,
                    now,
                ),
            )
            row = conn.execute(
                "SELECT * FROM obsidian_operations WHERE id=? AND user_id=?", (op_id, owner)
            ).fetchone()
            assert row is not None
            return dict(row), True

    def transition_obsidian_operation(
        self,
        user_id: str,
        operation_id: str,
        state: str,
        *,
        result: Mapping[str, Any] | None = None,
        delivery: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        owner = validate_user_id(user_id)
        with self.transaction() as conn:
            current = conn.execute(
                "SELECT * FROM obsidian_operations WHERE id=? AND user_id=?",
                (str(operation_id), owner),
            ).fetchone()
            if current is None:
                raise ValueError("Obsidian operation not found")
            old_state = str(current["status"])
            if state != old_state and state not in _OPERATION_TRANSITIONS.get(old_state, frozenset()):
                raise ValueError(f"invalid Obsidian operation transition: {old_state} -> {state}")
            result_json = str(current["result_json"]) if result is None else _canonical_json(result)
            delivery_json = str(current["delivery_json"]) if delivery is None else _canonical_json(delivery)
            conn.execute(
                """UPDATE obsidian_operations
                   SET status=?, result_json=?, delivery_json=?, updated_at=?
                   WHERE id=? AND user_id=?""",
                (state, result_json, delivery_json, utc_now(), str(operation_id), owner),
            )
            updated = conn.execute(
                "SELECT * FROM obsidian_operations WHERE id=? AND user_id=?",
                (str(operation_id), owner),
            ).fetchone()
            assert updated is not None
            return dict(updated)

    def get_obsidian_operation(self, user_id: str, operation_id: str) -> dict[str, Any] | None:
        owner = validate_user_id(user_id)
        return _row(
            self.execute(
                "SELECT * FROM obsidian_operations WHERE id=? AND user_id=?",
                (str(operation_id), owner),
            ).fetchone()
        )

    def list_pending_obsidian_operations(self, *, limit: int = 100) -> list[dict[str, Any]]:
        capped = min(500, max(1, int(limit)))
        rows = self.execute(
            """SELECT * FROM obsidian_operations
               WHERE status IN (
                   'committed', 'scan_pending', 'scan_complete',
                   'delivery_pending', 'uncertain'
               )
               ORDER BY updated_at, id LIMIT ?""",
            (capped,),
        ).fetchall()
        return [dict(item) for item in rows]

    def record_obsidian_conflict(
        self,
        user_id: str,
        *,
        vault_id: str,
        canonical_path: str,
        conflict_path: str,
    ) -> dict[str, Any]:
        owner = validate_user_id(user_id)
        if not str(canonical_path).strip() or not str(conflict_path).strip():
            raise ValueError("canonical_path and conflict_path are required")
        now = utc_now()
        with self.transaction() as conn:
            vault = conn.execute(
                "SELECT id FROM obsidian_vaults WHERE id=? AND user_id=?", (str(vault_id), owner)
            ).fetchone()
            if vault is None:
                raise ValueError("Obsidian vault not found")
            conn.execute(
                """INSERT INTO obsidian_conflicts(
                       id, user_id, vault_id, canonical_path, conflict_path,
                       detected_at, status, updated_at
                   ) VALUES(?, ?, ?, ?, ?, ?, 'open', ?)
                   ON CONFLICT(vault_id, conflict_path) DO UPDATE SET
                       canonical_path=excluded.canonical_path,
                       updated_at=excluded.updated_at""",
                (
                    new_id("obsconf"),
                    owner,
                    str(vault_id),
                    str(canonical_path),
                    str(conflict_path),
                    now,
                    now,
                ),
            )
            row = conn.execute(
                "SELECT * FROM obsidian_conflicts WHERE vault_id=? AND conflict_path=?",
                (str(vault_id), str(conflict_path)),
            ).fetchone()
            assert row is not None
            return dict(row)

    def list_obsidian_conflicts(self, user_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
        owner = validate_user_id(user_id)
        capped = min(500, max(1, int(limit)))
        rows = self.execute(
            """SELECT * FROM obsidian_conflicts
               WHERE user_id=? AND status='open'
               ORDER BY detected_at DESC, id DESC LIMIT ?""",
            (owner, capped),
        ).fetchall()
        return [dict(item) for item in rows]


__all__ = ["OBSIDIAN_SCHEMA", "ObsidianMixin", "validate_obsidian_schema"]
