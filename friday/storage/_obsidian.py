"""Durable state for the first-party Obsidian organ.

The note bodies live in the per-user vault checkout. SQLite stores only identity,
onboarding, operation and delivery facts; API keys remain in the private
Syncthing profile and are referenced, never copied into the database.
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import unicodedata
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from typing import Any

from friday.storage._base import StorageShared, validate_user_id
from friday.storage.models import new_id, utc_now

_OBSIDIAN_SCHEMA_VERSION = 36
_OBSIDIAN_SCHEMA_36_TABLES = frozenset(
    {
        "obsidian_note_bindings",
        "obsidian_note_index",
        "obsidian_note_links",
        "obsidian_candidate_sets",
        "obsidian_candidate_set_items",
        "obsidian_active_frames",
    }
)

# Released schema 35 is an immutable migration input.  Its only object changed
# in schema 36 is this table; every other old Obsidian object is recovered from
# the current canonical script after filtering the additive schema-36 tables.
# Keep this text byte-for-byte aligned with the 0.207.4 release: sqlite_master
# SQL is compared exactly before any migration DDL is allowed to run.
_OBSIDIAN_OPERATIONS_TABLE_SCHEMA_35 = """
CREATE TABLE obsidian_operations (
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
)
""".strip()

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
        'create', 'append', 'prepend', 'replace', 'move', 'delete',
        'set_properties', 'remove_properties', 'managed_region',
        'daily_note', 'verification_note', 'template', 'task', 'base',
        'conflict_merge', 'ingest'
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

CREATE TABLE IF NOT EXISTS obsidian_note_bindings (
    id TEXT NOT NULL,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    vault_id TEXT NOT NULL,
    integration_id TEXT NOT NULL,
    current_path TEXT NOT NULL,
    current_revision TEXT NOT NULL,
    ownership_mode TEXT NOT NULL
        CHECK(ownership_mode IN ('user_owned', 'linked', 'friday_managed', 'projection', 'inbox')),
    origin TEXT NOT NULL
        CHECK(origin IN ('user', 'android', 'friday', 'syncthing', 'projection', 'imported', 'unknown')),
    projection_kind TEXT,
    projection_json TEXT NOT NULL DEFAULT '{}',
    friday_object_kind TEXT,
    friday_object_id TEXT,
    deleted_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(user_id, id),
    CHECK(length(id) BETWEEN 1 AND 200 AND instr(id, char(0))=0),
    CHECK(length(integration_id) BETWEEN 1 AND 200 AND instr(integration_id, char(0))=0),
    CHECK(length(current_path) BETWEEN 1 AND 1024 AND instr(current_path, char(0))=0),
    CHECK(length(current_revision)=64 AND current_revision NOT GLOB '*[^0-9a-f]*'),
    CHECK(projection_kind IS NULL OR (
        length(projection_kind) BETWEEN 1 AND 100 AND instr(projection_kind, char(0))=0
    )),
    CHECK(json_valid(projection_json) AND json_type(projection_json)='object'),
    CHECK(length(CAST(projection_json AS BLOB))<=262144),
    CHECK((friday_object_kind IS NULL)=(friday_object_id IS NULL)),
    CHECK(friday_object_kind IS NULL OR (
        length(friday_object_kind) BETWEEN 1 AND 100
        AND instr(friday_object_kind, char(0))=0
        AND length(friday_object_id) BETWEEN 1 AND 200
        AND instr(friday_object_id, char(0))=0
    )),
    FOREIGN KEY(vault_id, user_id)
        REFERENCES obsidian_vaults(id, user_id) ON DELETE CASCADE
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_obsidian_note_binding_identity
    ON obsidian_note_bindings(id, user_id, vault_id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_obsidian_note_binding_integration
    ON obsidian_note_bindings(user_id, vault_id, integration_id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_obsidian_note_binding_active_path
    ON obsidian_note_bindings(user_id, vault_id, current_path)
    WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_obsidian_note_bindings_owner_updated
    ON obsidian_note_bindings(user_id, updated_at DESC, id);
CREATE TRIGGER IF NOT EXISTS obsidian_note_binding_identity_update_guard
BEFORE UPDATE OF id, user_id, vault_id, integration_id ON obsidian_note_bindings
WHEN NEW.id IS NOT OLD.id
  OR NEW.user_id IS NOT OLD.user_id
  OR NEW.vault_id IS NOT OLD.vault_id
  OR NEW.integration_id IS NOT OLD.integration_id
BEGIN
    SELECT RAISE(ABORT, 'obsidian note binding identity is immutable');
END;

CREATE TABLE IF NOT EXISTS obsidian_note_index (
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    binding_id TEXT NOT NULL,
    vault_id TEXT NOT NULL,
    revision TEXT NOT NULL,
    path TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    metadata_coverage TEXT NOT NULL DEFAULT 'none'
        CHECK(metadata_coverage IN ('none', 'partial', 'complete')),
    body_text TEXT NOT NULL DEFAULT '',
    body_coverage TEXT NOT NULL DEFAULT 'none'
        CHECK(body_coverage IN ('none', 'partial', 'complete')),
    source_size_bytes INTEGER NOT NULL DEFAULT 0,
    source_modified_at TEXT,
    state TEXT NOT NULL DEFAULT 'ready' CHECK(state IN ('ready', 'stale')),
    indexed_at TEXT NOT NULL,
    invalidated_at TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(user_id, binding_id),
    CHECK(length(revision)=64 AND revision NOT GLOB '*[^0-9a-f]*'),
    CHECK(length(path) BETWEEN 1 AND 1024 AND instr(path, char(0))=0),
    CHECK(length(title)<=512 AND instr(title, char(0))=0),
    CHECK(json_valid(metadata_json) AND json_type(metadata_json)='object'),
    CHECK(length(CAST(metadata_json AS BLOB))<=262144),
    CHECK(metadata_coverage<>'none' OR metadata_json='{}'),
    CHECK(source_size_bytes>=0),
    CHECK(length(CAST(body_text AS BLOB))<=4194304),
    CHECK(length(CAST(body_text AS BLOB))<=source_size_bytes),
    CHECK(body_coverage<>'none' OR body_text=''),
    CHECK(body_coverage<>'complete' OR length(CAST(body_text AS BLOB))=source_size_bytes),
    CHECK(body_coverage<>'partial' OR length(CAST(body_text AS BLOB))<source_size_bytes),
    CHECK((state='ready' AND invalidated_at IS NULL)
       OR (state='stale' AND invalidated_at IS NOT NULL)),
    FOREIGN KEY(binding_id, user_id, vault_id)
        REFERENCES obsidian_note_bindings(id, user_id, vault_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_obsidian_note_index_owner_state
    ON obsidian_note_index(user_id, state, updated_at DESC, binding_id);
CREATE INDEX IF NOT EXISTS idx_obsidian_note_index_owner_path
    ON obsidian_note_index(user_id, vault_id, path);

CREATE TABLE IF NOT EXISTS obsidian_note_links (
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    vault_id TEXT NOT NULL,
    source_binding_id TEXT NOT NULL,
    source_revision TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    link_kind TEXT NOT NULL
        CHECK(link_kind IN ('wikilink', 'markdown', 'embed', 'heading', 'block')),
    target_text TEXT NOT NULL,
    target_path TEXT,
    target_subpath TEXT,
    resolution_state TEXT NOT NULL
        CHECK(resolution_state IN ('resolved', 'unresolved', 'ambiguous', 'external')),
    resolved_binding_id TEXT,
    link_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(user_id, source_binding_id, source_revision, ordinal),
    CHECK(length(source_revision)=64 AND source_revision NOT GLOB '*[^0-9a-f]*'),
    CHECK(ordinal BETWEEN 1 AND 2048),
    CHECK(length(target_text) BETWEEN 1 AND 2048 AND instr(target_text, char(0))=0),
    CHECK(target_path IS NULL OR (
        length(target_path) BETWEEN 1 AND 1024 AND instr(target_path, char(0))=0
    )),
    CHECK(target_subpath IS NULL OR (
        length(target_subpath) BETWEEN 1 AND 512 AND instr(target_subpath, char(0))=0
    )),
    CHECK((resolution_state='resolved' AND resolved_binding_id IS NOT NULL)
       OR (resolution_state<>'resolved' AND resolved_binding_id IS NULL)),
    CHECK(json_valid(link_json) AND json_type(link_json)='object'),
    CHECK(length(CAST(link_json AS BLOB))<=262144),
    FOREIGN KEY(source_binding_id, user_id, vault_id)
        REFERENCES obsidian_note_bindings(id, user_id, vault_id) ON DELETE CASCADE,
    FOREIGN KEY(resolved_binding_id, user_id, vault_id)
        REFERENCES obsidian_note_bindings(id, user_id, vault_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_obsidian_note_links_backlinks
    ON obsidian_note_links(user_id, vault_id, resolved_binding_id, source_binding_id)
    WHERE resolution_state='resolved';
CREATE INDEX IF NOT EXISTS idx_obsidian_note_links_unresolved
    ON obsidian_note_links(user_id, vault_id, resolution_state, target_text)
    WHERE resolution_state IN ('unresolved', 'ambiguous');

CREATE TABLE IF NOT EXISTS obsidian_candidate_sets (
    id TEXT NOT NULL,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    vault_id TEXT NOT NULL,
    work_item_id TEXT,
    query_json TEXT NOT NULL,
    constraint_digest TEXT NOT NULL,
    coverage_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'active'
        CHECK(status IN ('active', 'selected', 'invalidated', 'expired')),
    selected_ordinal INTEGER,
    selected_binding_id TEXT,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    invalidated_at TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(user_id, id),
    CHECK(length(id) BETWEEN 1 AND 200 AND instr(id, char(0))=0),
    CHECK(work_item_id IS NULL OR (
        length(work_item_id) BETWEEN 1 AND 200 AND instr(work_item_id, char(0))=0
    )),
    CHECK(json_valid(query_json) AND json_type(query_json)='object'),
    CHECK(length(CAST(query_json AS BLOB))<=262144),
    CHECK(length(constraint_digest)=64 AND constraint_digest NOT GLOB '*[^0-9a-f]*'),
    CHECK(json_valid(coverage_json) AND json_type(coverage_json)='object'),
    CHECK(length(CAST(coverage_json AS BLOB))<=262144),
    CHECK(expires_at>created_at),
    CHECK((selected_ordinal IS NULL)=(selected_binding_id IS NULL)),
    CHECK((status='selected' AND selected_ordinal IS NOT NULL)
       OR (status<>'selected' AND selected_ordinal IS NULL)),
    CHECK((status IN ('active', 'selected') AND invalidated_at IS NULL)
       OR (status IN ('invalidated', 'expired') AND invalidated_at IS NOT NULL)),
    FOREIGN KEY(vault_id, user_id)
        REFERENCES obsidian_vaults(id, user_id) ON DELETE CASCADE,
    FOREIGN KEY(selected_binding_id, user_id, vault_id)
        REFERENCES obsidian_note_bindings(id, user_id, vault_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_obsidian_candidate_set_identity
    ON obsidian_candidate_sets(id, user_id, vault_id);
CREATE INDEX IF NOT EXISTS idx_obsidian_candidate_sets_owner_expiry
    ON obsidian_candidate_sets(user_id, status, expires_at, id);

CREATE TABLE IF NOT EXISTS obsidian_candidate_set_items (
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    candidate_set_id TEXT NOT NULL,
    vault_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    binding_id TEXT NOT NULL,
    observed_revision TEXT NOT NULL,
    observed_path TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    score REAL NOT NULL DEFAULT 0,
    match_channels_json TEXT NOT NULL DEFAULT '[]',
    candidate_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    PRIMARY KEY(user_id, candidate_set_id, ordinal),
    UNIQUE(user_id, candidate_set_id, binding_id),
    CHECK(ordinal BETWEEN 1 AND 100),
    CHECK(length(observed_revision)=64 AND observed_revision NOT GLOB '*[^0-9a-f]*'),
    CHECK(length(observed_path) BETWEEN 1 AND 1024 AND instr(observed_path, char(0))=0),
    CHECK(length(title)<=512 AND instr(title, char(0))=0),
    CHECK(score BETWEEN -1000000 AND 1000000),
    CHECK(json_valid(match_channels_json) AND json_type(match_channels_json)='array'),
    CHECK(length(CAST(match_channels_json AS BLOB))<=262144),
    CHECK(json_valid(candidate_json) AND json_type(candidate_json)='object'),
    CHECK(length(CAST(candidate_json AS BLOB))<=262144),
    FOREIGN KEY(candidate_set_id, user_id, vault_id)
        REFERENCES obsidian_candidate_sets(id, user_id, vault_id) ON DELETE CASCADE,
    FOREIGN KEY(binding_id, user_id, vault_id)
        REFERENCES obsidian_note_bindings(id, user_id, vault_id)
);

CREATE INDEX IF NOT EXISTS idx_obsidian_candidate_items_binding
    ON obsidian_candidate_set_items(user_id, vault_id, binding_id, candidate_set_id);
CREATE TRIGGER IF NOT EXISTS obsidian_candidate_selection_insert_guard
BEFORE INSERT ON obsidian_candidate_sets
WHEN NEW.selected_ordinal IS NOT NULL
 AND NOT EXISTS (
     SELECT 1 FROM obsidian_candidate_set_items item
      WHERE item.user_id=NEW.user_id
        AND item.candidate_set_id=NEW.id
        AND item.ordinal=NEW.selected_ordinal
        AND item.binding_id=NEW.selected_binding_id
 )
BEGIN
    SELECT RAISE(ABORT, 'obsidian candidate selection is not in its set');
END;
CREATE TRIGGER IF NOT EXISTS obsidian_candidate_selection_update_guard
BEFORE UPDATE OF selected_ordinal, selected_binding_id ON obsidian_candidate_sets
WHEN NEW.selected_ordinal IS NOT NULL
 AND NOT EXISTS (
     SELECT 1 FROM obsidian_candidate_set_items item
      WHERE item.user_id=NEW.user_id
        AND item.candidate_set_id=NEW.id
        AND item.ordinal=NEW.selected_ordinal
        AND item.binding_id=NEW.selected_binding_id
 )
BEGIN
    SELECT RAISE(ABORT, 'obsidian candidate selection is not in its set');
END;

CREATE TABLE IF NOT EXISTS obsidian_active_frames (
    id TEXT NOT NULL,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    vault_id TEXT NOT NULL,
    work_item_id TEXT,
    active_binding_id TEXT,
    active_path TEXT,
    active_revision TEXT,
    active_heading TEXT,
    candidate_set_id TEXT,
    selected_binding_id TEXT,
    selected_path TEXT,
    selected_revision TEXT,
    last_operation_id TEXT,
    frame_json TEXT NOT NULL DEFAULT '{}',
    state TEXT NOT NULL DEFAULT 'active' CHECK(state IN ('active', 'invalidated', 'expired')),
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    invalidated_at TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(user_id, id),
    CHECK(length(id) BETWEEN 1 AND 200 AND instr(id, char(0))=0),
    CHECK(work_item_id IS NULL OR (
        length(work_item_id) BETWEEN 1 AND 200 AND instr(work_item_id, char(0))=0
    )),
    CHECK((active_binding_id IS NULL)=(active_path IS NULL)
      AND (active_binding_id IS NULL)=(active_revision IS NULL)),
    CHECK(active_path IS NULL OR (
        length(active_path) BETWEEN 1 AND 1024 AND instr(active_path, char(0))=0
    )),
    CHECK(active_revision IS NULL OR (
        length(active_revision)=64 AND active_revision NOT GLOB '*[^0-9a-f]*'
    )),
    CHECK(active_heading IS NULL OR (
        length(active_heading) BETWEEN 1 AND 512 AND instr(active_heading, char(0))=0
    )),
    CHECK((selected_binding_id IS NULL)=(selected_path IS NULL)
      AND (selected_binding_id IS NULL)=(selected_revision IS NULL)),
    CHECK(selected_path IS NULL OR (
        length(selected_path) BETWEEN 1 AND 1024 AND instr(selected_path, char(0))=0
    )),
    CHECK(selected_revision IS NULL OR (
        length(selected_revision)=64 AND selected_revision NOT GLOB '*[^0-9a-f]*'
    )),
    CHECK(last_operation_id IS NULL OR (
        length(last_operation_id) BETWEEN 1 AND 200 AND instr(last_operation_id, char(0))=0
    )),
    CHECK(json_valid(frame_json) AND json_type(frame_json)='object'),
    CHECK(length(CAST(frame_json AS BLOB))<=262144),
    CHECK(expires_at>created_at),
    CHECK((state='active' AND invalidated_at IS NULL)
       OR (state IN ('invalidated', 'expired') AND invalidated_at IS NOT NULL)),
    FOREIGN KEY(vault_id, user_id)
        REFERENCES obsidian_vaults(id, user_id) ON DELETE CASCADE,
    FOREIGN KEY(active_binding_id, user_id, vault_id)
        REFERENCES obsidian_note_bindings(id, user_id, vault_id),
    FOREIGN KEY(selected_binding_id, user_id, vault_id)
        REFERENCES obsidian_note_bindings(id, user_id, vault_id),
    FOREIGN KEY(candidate_set_id, user_id, vault_id)
        REFERENCES obsidian_candidate_sets(id, user_id, vault_id),
    FOREIGN KEY(user_id, last_operation_id)
        REFERENCES obsidian_operations(user_id, id)
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_obsidian_active_frame_identity
    ON obsidian_active_frames(id, user_id, vault_id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_obsidian_active_frame_work_item
    ON obsidian_active_frames(user_id, work_item_id)
    WHERE work_item_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_obsidian_active_frames_owner_expiry
    ON obsidian_active_frames(user_id, state, expires_at, id);
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
    "prepared": frozenset({"committed", "reconciled", "conflict", "failed", "uncertain", "cancelled"}),
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

_OPERATION_RESULT_IMMUTABLE_STATES = frozenset(
    {"committed", "scan_pending", "scan_complete", "delivery_pending", "delivered", "reconciled"}
)

_NOTE_OWNERSHIP_MODES = frozenset({"user_owned", "linked", "friday_managed", "projection", "inbox"})
_NOTE_ORIGINS = frozenset({"user", "android", "friday", "syncthing", "projection", "imported", "unknown"})
_LINK_KINDS = frozenset({"wikilink", "markdown", "embed", "heading", "block"})
_LINK_RESOLUTION_STATES = frozenset({"resolved", "unresolved", "ambiguous", "external"})
_COVERAGE_STATES = frozenset({"none", "partial", "complete"})
_MAX_NOTE_BODY_BYTES = 4 * 1024 * 1024
_MAX_JSON_BYTES = 256 * 1024
_MAX_CANDIDATES = 100
_MAX_LINKS = 2048


def _bounded_text(
    value: object,
    field: str,
    *,
    maximum: int,
    allow_empty: bool = False,
) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    text = unicodedata.normalize("NFC", value.strip())
    if (not text and not allow_empty) or len(text) > maximum or "\x00" in text:
        qualifier = "possibly empty " if allow_empty else "non-empty "
        raise ValueError(f"{field} must be a bounded {qualifier}string")
    return text


def _optional_bounded_text(value: object | None, field: str, *, maximum: int) -> str | None:
    if value is None:
        return None
    return _bounded_text(value, field, maximum=maximum)


def _note_path(value: object, field: str = "path") -> str:
    path = _bounded_text(value, field, maximum=1024)
    if path.startswith("/") or "\\" in path:
        raise ValueError(f"{field} must be a relative POSIX vault path")
    parts = path.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError(f"{field} must not contain empty or traversal components")
    return path


def _revision(value: object, field: str = "revision") -> str:
    revision = str(value or "").strip().casefold()
    if len(revision) != 64 or any(character not in "0123456789abcdef" for character in revision):
        raise ValueError(f"{field} must be a lowercase SHA-256 digest")
    return revision


def _canonical_timestamp(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be an offset-aware timestamp")
    text = value.strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} must be an offset-aware timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} must be an offset-aware timestamp")
    return parsed.astimezone(UTC).isoformat(timespec="seconds")


def _expiry(
    expires_at: str | None,
    *,
    ttl_seconds: int,
    now: str | None,
) -> tuple[str, str]:
    current = _canonical_timestamp(now or utc_now(), "now")
    if expires_at is None:
        if (
            isinstance(ttl_seconds, bool)
            or not isinstance(ttl_seconds, int)
            or not 1 <= ttl_seconds <= 604800
        ):
            raise ValueError("ttl_seconds must be between 1 and 604800")
        parsed = datetime.fromisoformat(current)
        expiry = (parsed + timedelta(seconds=ttl_seconds)).isoformat(timespec="seconds")
    else:
        expiry = _canonical_timestamp(expires_at, "expires_at")
    if expiry <= current:
        raise ValueError("expires_at must be in the future")
    return current, expiry


def _json_object(value: Mapping[str, Any] | None, field: str) -> str:
    if value is not None and not isinstance(value, Mapping):
        raise ValueError(f"{field} must be a JSON object")
    try:
        encoded = json.dumps(
            dict(value or {}),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must contain only finite JSON values") from exc
    if len(encoded.encode("utf-8")) > _MAX_JSON_BYTES:
        raise ValueError(f"{field} exceeds the {_MAX_JSON_BYTES}-byte limit")
    return encoded


def _json_string_array(value: object, field: str, *, maximum: int = 64) -> str:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ValueError(f"{field} must be an array")
    if len(value) > maximum:
        raise ValueError(f"{field} has too many items")
    normalized = [_bounded_text(item, f"{field} item", maximum=100) for item in value]
    if len(set(normalized)) != len(normalized):
        raise ValueError(f"{field} contains duplicate items")
    return json.dumps(normalized, ensure_ascii=False, separators=(",", ":"))


def _limit(value: int, *, maximum: int = 500) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise ValueError(f"limit must be between 1 and {maximum}")
    return value


def _canonical_json(value: Mapping[str, Any] | None) -> str:
    return _json_object(value, "JSON payload")


def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


@lru_cache(maxsize=2)
def _canonical_schema_objects(schema_version: int = _OBSIDIAN_SCHEMA_VERSION) -> dict[tuple[str, str], str]:
    if schema_version not in {35, _OBSIDIAN_SCHEMA_VERSION}:
        raise ValueError(f"unsupported Obsidian schema version: {schema_version}")
    conn = sqlite3.connect(":memory:")
    try:
        conn.executescript(OBSIDIAN_SCHEMA)
        canonical = {
            (str(item[0]), str(item[1])): str(item[3])
            for item in conn.execute(
                """SELECT type, name, tbl_name, sql FROM sqlite_master
                   WHERE sql IS NOT NULL AND tbl_name LIKE 'obsidian_%'
                   ORDER BY type, name"""
            )
            if schema_version == _OBSIDIAN_SCHEMA_VERSION or str(item[2]) not in _OBSIDIAN_SCHEMA_36_TABLES
        }
        if schema_version == 35:
            canonical[("table", "obsidian_operations")] = _OBSIDIAN_OPERATIONS_TABLE_SCHEMA_35
        return canonical
    finally:
        conn.close()


def validate_obsidian_schema(
    conn: sqlite3.Connection,
    schema_version: int = _OBSIDIAN_SCHEMA_VERSION,
) -> None:
    """Reject an authoritative Obsidian schema or owner graph that was altered."""

    expected = _canonical_schema_objects(schema_version)
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
            f"Schema {schema_version} Obsidian state is incomplete or altered "
            f"(missing={len(missing)}, altered={len(altered)}, unexpected={len(unexpected)})"
        )

    # Exact DDL proves the guards still exist; this catches rows introduced with
    # foreign_keys disabled by an offline writer.  Stop at the first opaque
    # violation and never reflect attacker-controlled identifiers in the error.
    for table in sorted(name for object_type, name in expected if object_type == "table"):
        violation = conn.execute(
            f'PRAGMA foreign_key_check("{table}")'  # nosec B608 - canonical table allowlist above
        ).fetchone()
        if violation is not None:
            raise sqlite3.DatabaseError(f"Schema {schema_version} Obsidian state violates owner foreign keys")


def upgrade_obsidian_schema_35_to_36(conn: sqlite3.Connection) -> None:
    """Expand the released operation-method contract without trusting IF NOT EXISTS."""

    validate_obsidian_schema(conn, 35)
    current = _canonical_schema_objects(_OBSIDIAN_SCHEMA_VERSION)
    conn.execute("DROP INDEX idx_obsidian_operations_user_time")
    conn.execute("DROP INDEX idx_obsidian_operations_delivery")
    conn.execute("ALTER TABLE obsidian_operations RENAME TO obsidian_operations_schema35")
    conn.execute(current[("table", "obsidian_operations")])
    conn.execute(
        """INSERT INTO obsidian_operations(
               id, user_id, work_item_id, vault_id, method, arguments_digest,
               expected_revision, status, result_json, delivery_json,
               created_at, updated_at
           )
           SELECT id, user_id, work_item_id, vault_id, method, arguments_digest,
                  expected_revision, status, result_json, delivery_json,
                  created_at, updated_at
             FROM obsidian_operations_schema35"""
    )
    conn.execute("DROP TABLE obsidian_operations_schema35")
    conn.execute(current[("index", "idx_obsidian_operations_user_time")])
    conn.execute(current[("index", "idx_obsidian_operations_delivery")])


def _binding_by_id(
    conn: sqlite3.Connection,
    owner: str,
    binding_id: str,
    *,
    vault_id: str | None = None,
    include_deleted: bool = False,
) -> sqlite3.Row | None:
    clauses = ["id=?", "user_id=?"]
    params: list[Any] = [binding_id, owner]
    if vault_id is not None:
        clauses.append("vault_id=?")
        params.append(vault_id)
    if not include_deleted:
        clauses.append("deleted_at IS NULL")
    return conn.execute(
        f"SELECT * FROM obsidian_note_bindings WHERE {' AND '.join(clauses)}",  # nosec B608
        tuple(params),
    ).fetchone()


def _invalidate_candidate_frames(
    conn: sqlite3.Connection,
    owner: str,
    candidate_set_id: str,
    *,
    now: str,
) -> None:
    """Make an unusable candidate set unusable through every durable frame."""

    conn.execute(
        """UPDATE obsidian_active_frames
           SET state='invalidated', invalidated_at=?, updated_at=?
           WHERE user_id=? AND candidate_set_id=? AND state='active'""",
        (now, now, owner, candidate_set_id),
    )


def _invalidate_binding_dependents(
    conn: sqlite3.Connection,
    owner: str,
    binding_id: str,
    *,
    current_revision: str,
    current_path: str,
    now: str,
    deleted: bool,
) -> None:
    conn.execute(
        """UPDATE obsidian_note_index
           SET metadata_json='{}', metadata_coverage='none',
               body_text='', body_coverage='none', source_size_bytes=0,
               state='stale', invalidated_at=?, updated_at=?
           WHERE user_id=? AND binding_id=? AND state<>'stale'""",
        (now, now, owner, binding_id),
    )
    conn.execute(
        "DELETE FROM obsidian_note_links WHERE user_id=? AND source_binding_id=?",
        (owner, binding_id),
    )
    if deleted:
        conn.execute(
            """UPDATE obsidian_note_links
               SET resolution_state='unresolved', resolved_binding_id=NULL, updated_at=?
               WHERE user_id=? AND resolved_binding_id=?""",
            (now, owner, binding_id),
        )
    conn.execute(
        """UPDATE obsidian_candidate_sets
           SET status='invalidated', selected_ordinal=NULL, selected_binding_id=NULL,
               invalidated_at=?, updated_at=?
           WHERE user_id=? AND status IN ('active', 'selected')
             AND EXISTS (
                 SELECT 1 FROM obsidian_candidate_set_items item
                  WHERE item.user_id=obsidian_candidate_sets.user_id
                    AND item.candidate_set_id=obsidian_candidate_sets.id
                    AND item.binding_id=?
                    AND (? OR item.observed_revision<>? OR item.observed_path<>?)
             )""",
        (now, now, owner, binding_id, int(deleted), current_revision, current_path),
    )
    conn.execute(
        """UPDATE obsidian_active_frames
           SET state='invalidated', invalidated_at=?, updated_at=?
           WHERE user_id=? AND state='active' AND (
                 (active_binding_id=? AND (
                     ? OR active_revision<>? OR active_path<>?
                 ))
              OR (selected_binding_id=? AND (
                     ? OR selected_revision<>? OR selected_path<>?
                 ))
           )""",
        (
            now,
            now,
            owner,
            binding_id,
            int(deleted),
            current_revision,
            current_path,
            binding_id,
            int(deleted),
            current_revision,
            current_path,
        ),
    )
    conn.execute(
        """UPDATE obsidian_active_frames
           SET state='invalidated', invalidated_at=?, updated_at=?
           WHERE user_id=? AND state='active'
             AND candidate_set_id IN (
                 SELECT item.candidate_set_id
                   FROM obsidian_candidate_set_items item
                   JOIN obsidian_candidate_sets candidate_set
                     ON candidate_set.user_id=item.user_id
                    AND candidate_set.id=item.candidate_set_id
                  WHERE item.user_id=? AND item.binding_id=?
                    AND candidate_set.status='invalidated'
             )""",
        (now, now, owner, owner, binding_id),
    )


def _candidate_set_payload(conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    payload = dict(row)
    payload["items"] = [
        dict(item)
        for item in conn.execute(
            """SELECT * FROM obsidian_candidate_set_items
               WHERE user_id=? AND candidate_set_id=? ORDER BY ordinal""",
            (str(row["user_id"]), str(row["id"])),
        ).fetchall()
    ]
    return payload


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
        prepared_result: Mapping[str, Any] | None = None,
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
        prepared_payload = None if prepared_result is None else dict(prepared_result)
        prepared_json = "{}" if prepared_payload is None else _canonical_json(prepared_payload)
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
                if prepared_payload is not None and str(existing["status"]) in {"prepared", "uncertain"}:
                    current_result = json.loads(str(existing["result_json"] or "{}"))
                    if not isinstance(current_result, dict):
                        raise ValueError("prepared Obsidian operation result is invalid")
                    for key, value in prepared_payload.items():
                        if key in current_result and current_result[key] != value:
                            raise ValueError("prepared Obsidian operation target changed")
                    merged = {**current_result, **prepared_payload}
                    if merged != current_result:
                        conn.execute(
                            """UPDATE obsidian_operations SET result_json=?, updated_at=?
                               WHERE id=? AND user_id=?""",
                            (_canonical_json(merged), now, op_id, owner),
                        )
                        existing = conn.execute(
                            "SELECT * FROM obsidian_operations WHERE id=? AND user_id=?",
                            (op_id, owner),
                        ).fetchone()
                        assert existing is not None
                return dict(existing), False
            vault = conn.execute(
                "SELECT id FROM obsidian_vaults WHERE id=? AND user_id=?", (str(vault_id), owner)
            ).fetchone()
            if vault is None:
                raise ValueError("Obsidian vault not found")
            conn.execute(
                """INSERT INTO obsidian_operations(
                       id, user_id, work_item_id, vault_id, method, arguments_digest,
                       expected_revision, status, result_json, created_at, updated_at
                   ) VALUES(?, ?, ?, ?, ?, ?, ?, 'prepared', ?, ?, ?)""",
                (
                    op_id,
                    owner,
                    normalized_work_item,
                    str(vault_id),
                    str(method),
                    digest,
                    expected_revision,
                    prepared_json,
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
            if (
                result is not None
                and old_state in _OPERATION_RESULT_IMMUTABLE_STATES
                and result_json != str(current["result_json"])
            ):
                raise ValueError("accepted Obsidian operation result is immutable")
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

    def list_obsidian_operations(
        self,
        user_id: str,
        *,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Return one owner's recent operation ledger, newest first."""

        owner = validate_user_id(user_id)
        capped = min(100, max(1, int(limit)))
        rows = self.execute(
            """SELECT * FROM obsidian_operations
               WHERE user_id=?
               ORDER BY updated_at DESC, id DESC LIMIT ?""",
            (owner, capped),
        ).fetchall()
        return [dict(item) for item in rows]

    def list_pending_obsidian_operations(self, *, limit: int = 100) -> list[dict[str, Any]]:
        capped = min(500, max(1, int(limit)))
        rows = self.execute(
            """SELECT * FROM obsidian_operations
               WHERE status IN (
                   'prepared', 'committed', 'scan_pending', 'scan_complete',
                   'delivery_pending', 'uncertain'
               )
               ORDER BY updated_at, id LIMIT ?""",
            (capped,),
        ).fetchall()
        return [dict(item) for item in rows]

    def list_obsidian_legacy_marker_candidates(
        self,
        user_id: str,
        *,
        limit: int = 5_000,
    ) -> list[dict[str, Any]]:
        """Return bounded rows whose in-note legacy marker can be migrated safely."""

        owner = validate_user_id(user_id)
        capped = min(5_000, max(1, int(limit)))
        rows = self.execute(
            """SELECT * FROM obsidian_operations
               WHERE user_id=? AND status IN (
                   'committed', 'scan_pending', 'scan_complete',
                   'delivery_pending', 'delivered', 'reconciled',
                   'prepared', 'uncertain'
               )
               ORDER BY CASE WHEN status='delivered' THEN 1 ELSE 0 END,
                        updated_at, id LIMIT ?""",
            (owner, capped),
        ).fetchall()
        return [dict(item) for item in rows]

    def upsert_obsidian_note_binding(
        self,
        user_id: str,
        *,
        vault_id: str,
        integration_id: str,
        current_path: str,
        current_revision: str,
        ownership_mode: str = "user_owned",
        origin: str = "unknown",
        projection_kind: str | None = None,
        projection: Mapping[str, Any] | None = None,
        friday_object_kind: str | None = None,
        friday_object_id: str | None = None,
        expected_current_revision: str | None = None,
    ) -> dict[str, Any]:
        """Create or refresh one stable note identity and invalidate stale projections."""

        owner = validate_user_id(user_id)
        vault = _bounded_text(vault_id, "vault_id", maximum=200)
        integration = _bounded_text(integration_id, "integration_id", maximum=200)
        path = _note_path(current_path, "current_path")
        revision = _revision(current_revision, "current_revision")
        if ownership_mode not in _NOTE_OWNERSHIP_MODES:
            raise ValueError("invalid Obsidian ownership_mode")
        if origin not in _NOTE_ORIGINS:
            raise ValueError("invalid Obsidian note origin")
        projection_name = _optional_bounded_text(
            projection_kind,
            "projection_kind",
            maximum=100,
        )
        projection_json = _json_object(projection, "projection")
        object_kind = _optional_bounded_text(
            friday_object_kind,
            "friday_object_kind",
            maximum=100,
        )
        object_id = _optional_bounded_text(
            friday_object_id,
            "friday_object_id",
            maximum=200,
        )
        if (object_kind is None) != (object_id is None):
            raise ValueError("friday_object_kind and friday_object_id must be supplied together")
        expected = (
            _revision(expected_current_revision, "expected_current_revision")
            if expected_current_revision is not None
            else None
        )
        now = utc_now()
        with self.transaction() as conn:
            if (
                conn.execute(
                    "SELECT 1 FROM obsidian_vaults WHERE id=? AND user_id=?",
                    (vault, owner),
                ).fetchone()
                is None
            ):
                raise ValueError("Obsidian vault not found")
            existing = conn.execute(
                """SELECT * FROM obsidian_note_bindings
                   WHERE user_id=? AND vault_id=? AND integration_id=?""",
                (owner, vault, integration),
            ).fetchone()
            if existing is None and expected is not None:
                raise ValueError("Obsidian note binding not found for expected_current_revision")
            if (
                existing is not None
                and expected is not None
                and str(existing["current_revision"]) != expected
            ):
                raise ValueError("Obsidian note binding revision changed")
            binding_id = str(existing["id"]) if existing is not None else new_id("obsbind")
            created_at = str(existing["created_at"]) if existing is not None else now
            conn.execute(
                """INSERT INTO obsidian_note_bindings(
                       id, user_id, vault_id, integration_id, current_path,
                       current_revision, ownership_mode, origin, projection_kind,
                       projection_json, friday_object_kind, friday_object_id,
                       deleted_at, created_at, updated_at
                   ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)
                   ON CONFLICT(user_id, vault_id, integration_id) DO UPDATE SET
                       current_path=excluded.current_path,
                       current_revision=excluded.current_revision,
                       ownership_mode=excluded.ownership_mode,
                       origin=excluded.origin,
                       projection_kind=excluded.projection_kind,
                       projection_json=excluded.projection_json,
                       friday_object_kind=excluded.friday_object_kind,
                       friday_object_id=excluded.friday_object_id,
                       deleted_at=NULL,
                       updated_at=excluded.updated_at""",
                (
                    binding_id,
                    owner,
                    vault,
                    integration,
                    path,
                    revision,
                    ownership_mode,
                    origin,
                    projection_name,
                    projection_json,
                    object_kind,
                    object_id,
                    created_at,
                    now,
                ),
            )
            if existing is not None and (
                str(existing["current_revision"]) != revision
                or str(existing["current_path"]) != path
                or existing["deleted_at"] is not None
            ):
                _invalidate_binding_dependents(
                    conn,
                    owner,
                    binding_id,
                    current_revision=revision,
                    current_path=path,
                    now=now,
                    deleted=False,
                )
            row = _binding_by_id(conn, owner, binding_id, vault_id=vault)
            assert row is not None
            return dict(row)

    def get_obsidian_note_binding(
        self,
        user_id: str,
        integration_id: str,
        *,
        vault_id: str | None = None,
        include_deleted: bool = False,
    ) -> dict[str, Any] | None:
        owner = validate_user_id(user_id)
        integration = _bounded_text(integration_id, "integration_id", maximum=200)
        clauses = ["user_id=?", "integration_id=?"]
        params: list[Any] = [owner, integration]
        if vault_id is not None:
            clauses.append("vault_id=?")
            params.append(_bounded_text(vault_id, "vault_id", maximum=200))
        if not include_deleted:
            clauses.append("deleted_at IS NULL")
        return _row(
            self.execute(
                f"SELECT * FROM obsidian_note_bindings WHERE {' AND '.join(clauses)}",  # nosec B608
                tuple(params),
            ).fetchone()
        )

    def list_obsidian_note_bindings(
        self,
        user_id: str,
        *,
        vault_id: str | None = None,
        include_deleted: bool = False,
        limit: int = 5000,
    ) -> list[dict[str, Any]]:
        owner = validate_user_id(user_id)
        capped = _limit(limit, maximum=5000)
        clauses = ["user_id=?"]
        params: list[Any] = [owner]
        if vault_id is not None:
            clauses.append("vault_id=?")
            params.append(_bounded_text(vault_id, "vault_id", maximum=200))
        if not include_deleted:
            clauses.append("deleted_at IS NULL")
        params.append(capped)
        rows = self.execute(
            f"""SELECT * FROM obsidian_note_bindings
                WHERE {" AND ".join(clauses)}
                ORDER BY updated_at DESC, id LIMIT ?""",  # nosec B608
            tuple(params),
        ).fetchall()
        return [dict(row) for row in rows]

    def tombstone_obsidian_note_binding(
        self,
        user_id: str,
        integration_id: str,
        *,
        vault_id: str | None = None,
        expected_revision: str | None = None,
    ) -> dict[str, Any]:
        """Retain stable identity while making every continuation target unusable."""

        owner = validate_user_id(user_id)
        integration = _bounded_text(integration_id, "integration_id", maximum=200)
        expected = (
            _revision(expected_revision, "expected_revision") if expected_revision is not None else None
        )
        vault = _bounded_text(vault_id, "vault_id", maximum=200) if vault_id is not None else None
        with self.transaction() as conn:
            clauses = ["user_id=?", "integration_id=?"]
            params: list[Any] = [owner, integration]
            if vault is not None:
                clauses.append("vault_id=?")
                params.append(vault)
            row = conn.execute(
                f"SELECT * FROM obsidian_note_bindings WHERE {' AND '.join(clauses)}",  # nosec B608
                tuple(params),
            ).fetchone()
            if row is None:
                raise ValueError("Obsidian note binding not found")
            if expected is not None and str(row["current_revision"]) != expected:
                raise ValueError("Obsidian note binding revision changed")
            if row["deleted_at"] is None:
                now = utc_now()
                conn.execute(
                    """UPDATE obsidian_note_bindings
                       SET deleted_at=?, updated_at=? WHERE user_id=? AND id=?""",
                    (now, now, owner, str(row["id"])),
                )
                _invalidate_binding_dependents(
                    conn,
                    owner,
                    str(row["id"]),
                    current_revision=str(row["current_revision"]),
                    current_path=str(row["current_path"]),
                    now=now,
                    deleted=True,
                )
            updated = _binding_by_id(
                conn,
                owner,
                str(row["id"]),
                include_deleted=True,
            )
            assert updated is not None
            return dict(updated)

    def upsert_obsidian_note_index(
        self,
        user_id: str,
        *,
        binding_id: str,
        revision: str,
        metadata: Mapping[str, Any] | None = None,
        metadata_coverage: str = "complete",
        body_text: str | None = None,
        body_coverage: str | None = None,
        source_size_bytes: int | None = None,
        title: str = "",
        source_modified_at: str | None = None,
    ) -> dict[str, Any]:
        """Publish a bounded index row only for the binding's current revision."""

        owner = validate_user_id(user_id)
        opaque_id = _bounded_text(binding_id, "binding_id", maximum=200)
        observed_revision = _revision(revision)
        if metadata_coverage not in _COVERAGE_STATES:
            raise ValueError("invalid metadata_coverage")
        metadata_json = _json_object(metadata, "metadata")
        if metadata_coverage == "none" and metadata_json != "{}":
            raise ValueError("metadata must be empty when metadata_coverage is none")
        if body_text is None:
            body = ""
            coverage = body_coverage or "none"
        else:
            if not isinstance(body_text, str) or "\x00" in body_text:
                raise ValueError("body_text must be UTF-8 text without NUL")
            body = body_text
            coverage = body_coverage or "complete"
        if coverage not in _COVERAGE_STATES:
            raise ValueError("invalid body_coverage")
        stored_bytes = len(body.encode("utf-8"))
        if stored_bytes > _MAX_NOTE_BODY_BYTES:
            raise ValueError("body_text exceeds the 4 MiB index limit")
        if isinstance(source_size_bytes, bool) or (
            source_size_bytes is not None and not isinstance(source_size_bytes, int)
        ):
            raise ValueError("source_size_bytes must be an integer")
        source_bytes = stored_bytes if source_size_bytes is None else source_size_bytes
        if source_bytes < stored_bytes or source_bytes < 0:
            raise ValueError("source_size_bytes cannot be smaller than indexed body bytes")
        if coverage == "none" and body:
            raise ValueError("body_text must be empty when body_coverage is none")
        if coverage == "complete" and source_bytes != stored_bytes:
            raise ValueError("complete body coverage must equal source_size_bytes")
        if coverage == "partial" and stored_bytes >= source_bytes:
            raise ValueError("partial body coverage must omit at least one source byte")
        normalized_title = _bounded_text(title, "title", maximum=512, allow_empty=True)
        modified_at = (
            _canonical_timestamp(source_modified_at, "source_modified_at")
            if source_modified_at is not None
            else None
        )
        now = utc_now()
        with self.transaction() as conn:
            binding = _binding_by_id(conn, owner, opaque_id)
            if binding is None:
                raise ValueError("Obsidian note binding not found")
            if str(binding["current_revision"]) != observed_revision:
                raise ValueError("cannot index a stale Obsidian note revision")
            conn.execute(
                """INSERT INTO obsidian_note_index(
                       user_id, binding_id, vault_id, revision, path, title,
                       metadata_json, metadata_coverage, body_text, body_coverage,
                       source_size_bytes, source_modified_at, state, indexed_at,
                       invalidated_at, updated_at
                   ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'ready', ?, NULL, ?)
                   ON CONFLICT(user_id, binding_id) DO UPDATE SET
                       vault_id=excluded.vault_id,
                       revision=excluded.revision,
                       path=excluded.path,
                       title=excluded.title,
                       metadata_json=excluded.metadata_json,
                       metadata_coverage=excluded.metadata_coverage,
                       body_text=excluded.body_text,
                       body_coverage=excluded.body_coverage,
                       source_size_bytes=excluded.source_size_bytes,
                       source_modified_at=excluded.source_modified_at,
                       state='ready', indexed_at=excluded.indexed_at,
                       invalidated_at=NULL, updated_at=excluded.updated_at""",
                (
                    owner,
                    opaque_id,
                    str(binding["vault_id"]),
                    observed_revision,
                    str(binding["current_path"]),
                    normalized_title,
                    metadata_json,
                    metadata_coverage,
                    body,
                    coverage,
                    source_bytes,
                    modified_at,
                    now,
                    now,
                ),
            )
            row = conn.execute(
                "SELECT * FROM obsidian_note_index WHERE user_id=? AND binding_id=?",
                (owner, opaque_id),
            ).fetchone()
            assert row is not None
            return dict(row)

    def get_obsidian_note_index(
        self,
        user_id: str,
        binding_id: str,
        *,
        include_stale: bool = False,
    ) -> dict[str, Any] | None:
        owner = validate_user_id(user_id)
        opaque_id = _bounded_text(binding_id, "binding_id", maximum=200)
        row = self.execute(
            """SELECT idx.* FROM obsidian_note_index idx
               JOIN obsidian_note_bindings binding
                 ON binding.id=idx.binding_id
                AND binding.user_id=idx.user_id
                AND binding.vault_id=idx.vault_id
               WHERE idx.user_id=? AND idx.binding_id=?
                 AND binding.deleted_at IS NULL
                 AND (? OR idx.state='ready')""",
            (owner, opaque_id, int(include_stale)),
        ).fetchone()
        return _row(row)

    def list_obsidian_note_index(
        self,
        user_id: str,
        *,
        vault_id: str | None = None,
        include_stale: bool = False,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        owner = validate_user_id(user_id)
        capped = _limit(limit)
        clauses = ["idx.user_id=?", "binding.deleted_at IS NULL"]
        params: list[Any] = [owner]
        if vault_id is not None:
            clauses.append("idx.vault_id=?")
            params.append(_bounded_text(vault_id, "vault_id", maximum=200))
        if not include_stale:
            clauses.append("idx.state='ready'")
        params.append(capped)
        rows = self.execute(
            f"""SELECT idx.* FROM obsidian_note_index idx
                JOIN obsidian_note_bindings binding
                  ON binding.id=idx.binding_id
                 AND binding.user_id=idx.user_id
                 AND binding.vault_id=idx.vault_id
                WHERE {" AND ".join(clauses)}
                ORDER BY idx.updated_at DESC, idx.binding_id LIMIT ?""",  # nosec B608
            tuple(params),
        ).fetchall()
        return [dict(row) for row in rows]

    def invalidate_obsidian_note_index(
        self,
        user_id: str,
        binding_id: str,
        *,
        expected_revision: str | None = None,
    ) -> dict[str, Any] | None:
        owner = validate_user_id(user_id)
        opaque_id = _bounded_text(binding_id, "binding_id", maximum=200)
        expected = (
            _revision(expected_revision, "expected_revision") if expected_revision is not None else None
        )
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM obsidian_note_index WHERE user_id=? AND binding_id=?",
                (owner, opaque_id),
            ).fetchone()
            if row is None:
                return None
            if expected is not None and str(row["revision"]) != expected:
                raise ValueError("Obsidian note index revision changed")
            if str(row["state"]) != "stale":
                now = utc_now()
                conn.execute(
                    """UPDATE obsidian_note_index
                       SET metadata_json='{}', metadata_coverage='none',
                           body_text='', body_coverage='none', source_size_bytes=0,
                           state='stale', invalidated_at=?, updated_at=?
                       WHERE user_id=? AND binding_id=?""",
                    (now, now, owner, opaque_id),
                )
            updated = conn.execute(
                "SELECT * FROM obsidian_note_index WHERE user_id=? AND binding_id=?",
                (owner, opaque_id),
            ).fetchone()
            assert updated is not None
            return dict(updated)

    def replace_obsidian_note_links(
        self,
        user_id: str,
        *,
        binding_id: str,
        revision: str,
        links: Sequence[Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        """Atomically replace the complete outgoing-link snapshot for one revision."""

        owner = validate_user_id(user_id)
        opaque_id = _bounded_text(binding_id, "binding_id", maximum=200)
        observed_revision = _revision(revision)
        if isinstance(links, (str, bytes)) or not isinstance(links, Sequence):
            raise ValueError("links must be an array")
        if len(links) > _MAX_LINKS:
            raise ValueError(f"links cannot contain more than {_MAX_LINKS} items")
        normalized: list[tuple[str, str, str, str | None, str | None, str, str | None, str]] = []
        with self.transaction() as conn:
            source = _binding_by_id(conn, owner, opaque_id)
            if source is None:
                raise ValueError("Obsidian source note binding not found")
            if str(source["current_revision"]) != observed_revision:
                raise ValueError("cannot publish links for a stale Obsidian note revision")
            vault = str(source["vault_id"])
            for item in links:
                if not isinstance(item, Mapping):
                    raise ValueError("each link must be an object")
                kind = str(item.get("link_kind", item.get("kind", ""))).strip()
                if kind not in _LINK_KINDS:
                    raise ValueError("invalid Obsidian link_kind")
                target_text = _bounded_text(item.get("target_text"), "target_text", maximum=2048)
                target_path = _optional_bounded_text(
                    item.get("target_path"),
                    "target_path",
                    maximum=1024,
                )
                target_subpath = _optional_bounded_text(
                    item.get("target_subpath"),
                    "target_subpath",
                    maximum=512,
                )
                resolved_id_value = item.get("resolved_binding_id")
                resolved_id = (
                    _bounded_text(resolved_id_value, "resolved_binding_id", maximum=200)
                    if resolved_id_value is not None
                    else None
                )
                state = str(
                    item.get(
                        "resolution_state",
                        "resolved" if resolved_id is not None else "unresolved",
                    )
                ).strip()
                if state not in _LINK_RESOLUTION_STATES:
                    raise ValueError("invalid Obsidian link resolution_state")
                if (state == "resolved") != (resolved_id is not None):
                    raise ValueError("resolved links require exactly one resolved_binding_id")
                if resolved_id is not None:
                    target = _binding_by_id(conn, owner, resolved_id, vault_id=vault)
                    if target is None:
                        raise ValueError("resolved Obsidian link target not found in the owner vault")
                detail = item.get("link", item.get("metadata"))
                detail_json = _json_object(
                    detail if isinstance(detail, Mapping) else None,
                    "link metadata",
                )
                if detail is not None and not isinstance(detail, Mapping):
                    raise ValueError("link metadata must be an object")
                normalized.append(
                    (
                        kind,
                        target_text,
                        state,
                        target_path,
                        target_subpath,
                        vault,
                        resolved_id,
                        detail_json,
                    )
                )
            conn.execute(
                "DELETE FROM obsidian_note_links WHERE user_id=? AND source_binding_id=?",
                (owner, opaque_id),
            )
            now = utc_now()
            conn.executemany(
                """INSERT INTO obsidian_note_links(
                       user_id, vault_id, source_binding_id, source_revision,
                       ordinal, link_kind, target_text, target_path,
                       target_subpath, resolution_state, resolved_binding_id,
                       link_json, created_at, updated_at
                   ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    (
                        owner,
                        vault,
                        opaque_id,
                        observed_revision,
                        position,
                        kind,
                        target_text,
                        target_path,
                        target_subpath,
                        state,
                        resolved_id,
                        detail_json,
                        now,
                        now,
                    )
                    for position, (
                        kind,
                        target_text,
                        state,
                        target_path,
                        target_subpath,
                        vault,
                        resolved_id,
                        detail_json,
                    ) in enumerate(normalized, start=1)
                ],
            )
            return [
                dict(row)
                for row in conn.execute(
                    """SELECT * FROM obsidian_note_links
                       WHERE user_id=? AND source_binding_id=?
                       ORDER BY ordinal""",
                    (owner, opaque_id),
                ).fetchall()
            ]

    def list_obsidian_note_links(
        self,
        user_id: str,
        *,
        source_binding_id: str | None = None,
        target_binding_id: str | None = None,
        resolution_state: str | None = None,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        owner = validate_user_id(user_id)
        capped = _limit(limit)
        clauses = ["links.user_id=?", "source.deleted_at IS NULL"]
        params: list[Any] = [owner]
        if source_binding_id is not None:
            clauses.append("links.source_binding_id=?")
            params.append(_bounded_text(source_binding_id, "source_binding_id", maximum=200))
        if target_binding_id is not None:
            clauses.append("links.resolved_binding_id=?")
            params.append(_bounded_text(target_binding_id, "target_binding_id", maximum=200))
        if resolution_state is not None:
            if resolution_state not in _LINK_RESOLUTION_STATES:
                raise ValueError("invalid Obsidian link resolution_state")
            clauses.append("links.resolution_state=?")
            params.append(resolution_state)
        params.append(capped)
        rows = self.execute(
            f"""SELECT links.* FROM obsidian_note_links links
                JOIN obsidian_note_bindings source
                  ON source.id=links.source_binding_id
                 AND source.user_id=links.user_id
                 AND source.vault_id=links.vault_id
                WHERE {" AND ".join(clauses)}
                  AND links.source_revision=source.current_revision
                ORDER BY links.updated_at DESC, links.source_binding_id, links.ordinal
                LIMIT ?""",  # nosec B608
            tuple(params),
        ).fetchall()
        return [dict(row) for row in rows]

    def create_obsidian_candidate_set(
        self,
        user_id: str,
        *,
        vault_id: str,
        query: Mapping[str, Any],
        candidates: Sequence[Mapping[str, Any]],
        coverage: Mapping[str, Any] | None = None,
        constraint_digest: str | None = None,
        work_item_id: str | None = None,
        candidate_set_id: str | None = None,
        expires_at: str | None = None,
        ttl_seconds: int = 900,
        now: str | None = None,
    ) -> dict[str, Any]:
        """Persist one ordered, revision-pinned result set for bounded follow-ups."""

        owner = validate_user_id(user_id)
        vault = _bounded_text(vault_id, "vault_id", maximum=200)
        query_json = _json_object(query, "query")
        coverage_json = _json_object(coverage, "coverage")
        digest = (
            _revision(constraint_digest, "constraint_digest")
            if constraint_digest is not None
            else hashlib.sha256(query_json.encode("utf-8")).hexdigest()
        )
        work_item = _optional_bounded_text(work_item_id, "work_item_id", maximum=200)
        set_id = (
            _bounded_text(candidate_set_id, "candidate_set_id", maximum=200)
            if candidate_set_id is not None
            else new_id("obscset")
        )
        if isinstance(candidates, (str, bytes)) or not isinstance(candidates, Sequence):
            raise ValueError("candidates must be an array")
        if len(candidates) > _MAX_CANDIDATES:
            raise ValueError(f"candidate set cannot exceed {_MAX_CANDIDATES} items")
        created_at, expiry = _expiry(expires_at, ttl_seconds=ttl_seconds, now=now)
        with self.transaction() as conn:
            if (
                conn.execute(
                    "SELECT 1 FROM obsidian_vaults WHERE id=? AND user_id=?",
                    (vault, owner),
                ).fetchone()
                is None
            ):
                raise ValueError("Obsidian vault not found")
            normalized: list[tuple[int, str, str, str, str, float, str, str]] = []
            seen_bindings: set[str] = set()
            known_fields = {
                "binding_id",
                "note_binding_id",
                "observed_revision",
                "revision",
                "observed_path",
                "path",
                "title",
                "score",
                "match_channels",
            }
            for ordinal, item in enumerate(candidates, start=1):
                if not isinstance(item, Mapping):
                    raise ValueError("each candidate must be an object")
                raw_binding_id = item.get("binding_id", item.get("note_binding_id"))
                binding_id = _bounded_text(raw_binding_id, "candidate binding_id", maximum=200)
                if binding_id in seen_bindings:
                    raise ValueError("candidate set contains a duplicate binding")
                seen_bindings.add(binding_id)
                binding = _binding_by_id(conn, owner, binding_id, vault_id=vault)
                if binding is None:
                    raise ValueError("candidate binding not found in the owner vault")
                observed_revision = _revision(
                    item.get("observed_revision", item.get("revision", binding["current_revision"])),
                    "candidate observed_revision",
                )
                observed_path = _note_path(
                    item.get("observed_path", item.get("path", binding["current_path"])),
                    "candidate observed_path",
                )
                if observed_revision != str(binding["current_revision"]) or observed_path != str(
                    binding["current_path"]
                ):
                    raise ValueError("candidate must pin the binding's current path and revision")
                title = _bounded_text(
                    item.get("title", ""),
                    "candidate title",
                    maximum=512,
                    allow_empty=True,
                )
                raw_score = item.get("score", 0.0)
                if isinstance(raw_score, bool) or not isinstance(raw_score, (int, float)):
                    raise ValueError("candidate score must be a finite number")
                score = float(raw_score)
                if not math.isfinite(score) or not -1_000_000 <= score <= 1_000_000:
                    raise ValueError("candidate score must be a bounded finite number")
                channels_json = _json_string_array(
                    item.get("match_channels", []),
                    "match_channels",
                )
                detail_json = _json_object(
                    {key: value for key, value in item.items() if key not in known_fields},
                    "candidate metadata",
                )
                normalized.append(
                    (
                        ordinal,
                        binding_id,
                        observed_revision,
                        observed_path,
                        title,
                        score,
                        channels_json,
                        detail_json,
                    )
                )
            conn.execute(
                """INSERT INTO obsidian_candidate_sets(
                       id, user_id, vault_id, work_item_id, query_json,
                       constraint_digest, coverage_json, status,
                       selected_ordinal, selected_binding_id, created_at,
                       expires_at, invalidated_at, updated_at
                   ) VALUES(?, ?, ?, ?, ?, ?, ?, 'active', NULL, NULL, ?, ?, NULL, ?)""",
                (
                    set_id,
                    owner,
                    vault,
                    work_item,
                    query_json,
                    digest,
                    coverage_json,
                    created_at,
                    expiry,
                    created_at,
                ),
            )
            conn.executemany(
                """INSERT INTO obsidian_candidate_set_items(
                       user_id, candidate_set_id, vault_id, ordinal, binding_id,
                       observed_revision, observed_path, title, score,
                       match_channels_json, candidate_json, created_at
                   ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    (
                        owner,
                        set_id,
                        vault,
                        ordinal,
                        binding_id,
                        observed_revision,
                        observed_path,
                        title,
                        score,
                        channels_json,
                        detail_json,
                        created_at,
                    )
                    for (
                        ordinal,
                        binding_id,
                        observed_revision,
                        observed_path,
                        title,
                        score,
                        channels_json,
                        detail_json,
                    ) in normalized
                ],
            )
            row = conn.execute(
                "SELECT * FROM obsidian_candidate_sets WHERE user_id=? AND id=?",
                (owner, set_id),
            ).fetchone()
            assert row is not None
            return _candidate_set_payload(conn, row)

    def get_obsidian_candidate_set(
        self,
        user_id: str,
        candidate_set_id: str,
        *,
        now: str | None = None,
        include_inactive: bool = False,
    ) -> dict[str, Any] | None:
        owner = validate_user_id(user_id)
        set_id = _bounded_text(candidate_set_id, "candidate_set_id", maximum=200)
        current = _canonical_timestamp(now or utc_now(), "now")
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM obsidian_candidate_sets WHERE user_id=? AND id=?",
                (owner, set_id),
            ).fetchone()
            if row is None:
                return None
            status = str(row["status"])
            if status in {"active", "selected"} and str(row["expires_at"]) <= current:
                conn.execute(
                    """UPDATE obsidian_candidate_sets
                       SET status='expired', selected_ordinal=NULL,
                           selected_binding_id=NULL, invalidated_at=?, updated_at=?
                       WHERE user_id=? AND id=?""",
                    (current, current, owner, set_id),
                )
                _invalidate_candidate_frames(conn, owner, set_id, now=current)
                status = "expired"
            if status in {"active", "selected"}:
                stale = conn.execute(
                    """SELECT 1 FROM obsidian_candidate_set_items item
                       LEFT JOIN obsidian_note_bindings binding
                         ON binding.id=item.binding_id
                        AND binding.user_id=item.user_id
                        AND binding.vault_id=item.vault_id
                       WHERE item.user_id=? AND item.candidate_set_id=?
                         AND (binding.id IS NULL OR binding.deleted_at IS NOT NULL
                              OR binding.current_revision<>item.observed_revision
                              OR binding.current_path<>item.observed_path)
                       LIMIT 1""",
                    (owner, set_id),
                ).fetchone()
                if stale is not None:
                    conn.execute(
                        """UPDATE obsidian_candidate_sets
                           SET status='invalidated', selected_ordinal=NULL,
                               selected_binding_id=NULL, invalidated_at=?, updated_at=?
                           WHERE user_id=? AND id=?""",
                        (current, current, owner, set_id),
                    )
                    _invalidate_candidate_frames(conn, owner, set_id, now=current)
                    status = "invalidated"
            if status not in {"active", "selected"} and not include_inactive:
                return None
            current_row = conn.execute(
                "SELECT * FROM obsidian_candidate_sets WHERE user_id=? AND id=?",
                (owner, set_id),
            ).fetchone()
            assert current_row is not None
            return _candidate_set_payload(conn, current_row)

    def select_obsidian_candidate(
        self,
        user_id: str,
        candidate_set_id: str,
        ordinal: int,
        *,
        now: str | None = None,
    ) -> dict[str, Any]:
        """Select an observed target only while its set, path and revision remain current."""

        owner = validate_user_id(user_id)
        set_id = _bounded_text(candidate_set_id, "candidate_set_id", maximum=200)
        if isinstance(ordinal, bool) or not isinstance(ordinal, int) or not 1 <= ordinal <= _MAX_CANDIDATES:
            raise ValueError(f"ordinal must be between 1 and {_MAX_CANDIDATES}")
        current = _canonical_timestamp(now or utc_now(), "now")
        failure: str | None = None
        selected_payload: dict[str, Any] | None = None
        with self.transaction() as conn:
            candidate_set = conn.execute(
                """SELECT * FROM obsidian_candidate_sets
                   WHERE user_id=? AND id=? AND status IN ('active', 'selected')""",
                (owner, set_id),
            ).fetchone()
            if candidate_set is None:
                raise ValueError("active Obsidian candidate set not found")
            if str(candidate_set["expires_at"]) <= current:
                conn.execute(
                    """UPDATE obsidian_candidate_sets
                       SET status='expired', selected_ordinal=NULL,
                           selected_binding_id=NULL, invalidated_at=?, updated_at=?
                       WHERE user_id=? AND id=?""",
                    (current, current, owner, set_id),
                )
                _invalidate_candidate_frames(conn, owner, set_id, now=current)
                failure = "Obsidian candidate set expired"
            else:
                item = conn.execute(
                    """SELECT item.*, binding.current_revision, binding.current_path,
                              binding.deleted_at
                         FROM obsidian_candidate_set_items item
                         LEFT JOIN obsidian_note_bindings binding
                           ON binding.id=item.binding_id
                          AND binding.user_id=item.user_id
                          AND binding.vault_id=item.vault_id
                        WHERE item.user_id=? AND item.candidate_set_id=? AND item.ordinal=?""",
                    (owner, set_id, ordinal),
                ).fetchone()
                if item is None:
                    raise ValueError("Obsidian candidate ordinal not found")
                if (
                    item["deleted_at"] is not None
                    or str(item["current_revision"] or "") != str(item["observed_revision"])
                    or str(item["current_path"] or "") != str(item["observed_path"])
                ):
                    conn.execute(
                        """UPDATE obsidian_candidate_sets
                           SET status='invalidated', selected_ordinal=NULL,
                               selected_binding_id=NULL, invalidated_at=?, updated_at=?
                           WHERE user_id=? AND id=?""",
                        (current, current, owner, set_id),
                    )
                    _invalidate_candidate_frames(conn, owner, set_id, now=current)
                    failure = "Obsidian candidate target is stale or deleted"
                else:
                    conn.execute(
                        """UPDATE obsidian_candidate_sets
                           SET status='selected', selected_ordinal=?, selected_binding_id=?,
                               invalidated_at=NULL, updated_at=?
                           WHERE user_id=? AND id=?""",
                        (ordinal, str(item["binding_id"]), current, owner, set_id),
                    )
                    conn.execute(
                        """UPDATE obsidian_active_frames
                           SET selected_binding_id=?, selected_path=?, selected_revision=?,
                               updated_at=?
                           WHERE user_id=? AND candidate_set_id=? AND state='active'""",
                        (
                            str(item["binding_id"]),
                            str(item["observed_path"]),
                            str(item["observed_revision"]),
                            current,
                            owner,
                            set_id,
                        ),
                    )
                    selected_payload = {
                        key: value
                        for key, value in dict(item).items()
                        if key not in {"current_revision", "current_path", "deleted_at"}
                    }
        if failure is not None:
            raise ValueError(failure)
        assert selected_payload is not None
        return selected_payload

    def invalidate_obsidian_candidate_set(
        self,
        user_id: str,
        candidate_set_id: str,
    ) -> dict[str, Any] | None:
        owner = validate_user_id(user_id)
        set_id = _bounded_text(candidate_set_id, "candidate_set_id", maximum=200)
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM obsidian_candidate_sets WHERE user_id=? AND id=?",
                (owner, set_id),
            ).fetchone()
            if row is None:
                return None
            if str(row["status"]) in {"active", "selected"}:
                now = utc_now()
                conn.execute(
                    """UPDATE obsidian_candidate_sets
                       SET status='invalidated', selected_ordinal=NULL,
                           selected_binding_id=NULL, invalidated_at=?, updated_at=?
                       WHERE user_id=? AND id=?""",
                    (now, now, owner, set_id),
                )
                _invalidate_candidate_frames(conn, owner, set_id, now=now)
            updated = conn.execute(
                "SELECT * FROM obsidian_candidate_sets WHERE user_id=? AND id=?",
                (owner, set_id),
            ).fetchone()
            assert updated is not None
            return _candidate_set_payload(conn, updated)

    def upsert_obsidian_active_frame(
        self,
        user_id: str,
        *,
        vault_id: str,
        frame_id: str | None = None,
        work_item_id: str | None = None,
        active_binding_id: str | None = None,
        active_heading: str | None = None,
        candidate_set_id: str | None = None,
        selected_binding_id: str | None = None,
        last_operation_id: str | None = None,
        frame: Mapping[str, Any] | None = None,
        expires_at: str | None = None,
        ttl_seconds: int = 900,
        now: str | None = None,
    ) -> dict[str, Any]:
        """Upsert one expiring operational frame after proving every referenced target."""

        owner = validate_user_id(user_id)
        vault = _bounded_text(vault_id, "vault_id", maximum=200)
        work_item = _optional_bounded_text(work_item_id, "work_item_id", maximum=200)
        if frame_id is None and work_item is None:
            raise ValueError("frame_id or work_item_id is required")
        opaque_id = _bounded_text(
            frame_id if frame_id is not None else work_item,
            "frame_id",
            maximum=200,
        )
        active_id = _optional_bounded_text(active_binding_id, "active_binding_id", maximum=200)
        heading = _optional_bounded_text(active_heading, "active_heading", maximum=512)
        set_id = _optional_bounded_text(candidate_set_id, "candidate_set_id", maximum=200)
        selected_id = _optional_bounded_text(
            selected_binding_id,
            "selected_binding_id",
            maximum=200,
        )
        operation_id = _optional_bounded_text(
            last_operation_id,
            "last_operation_id",
            maximum=200,
        )
        frame_json = _json_object(frame, "frame")
        current, expiry = _expiry(expires_at, ttl_seconds=ttl_seconds, now=now)
        with self.transaction() as conn:
            if (
                conn.execute(
                    "SELECT 1 FROM obsidian_vaults WHERE id=? AND user_id=?",
                    (vault, owner),
                ).fetchone()
                is None
            ):
                raise ValueError("Obsidian vault not found")
            active = _binding_by_id(conn, owner, active_id, vault_id=vault) if active_id is not None else None
            if active_id is not None and active is None:
                raise ValueError("active Obsidian note binding not found")
            candidate_set = None
            if set_id is not None:
                candidate_set = conn.execute(
                    """SELECT * FROM obsidian_candidate_sets
                       WHERE id=? AND user_id=? AND vault_id=?
                         AND status IN ('active', 'selected') AND expires_at>?""",
                    (set_id, owner, vault, current),
                ).fetchone()
                if candidate_set is None:
                    raise ValueError("active Obsidian candidate set not found")
                candidate_selected = candidate_set["selected_binding_id"]
                if selected_id is None and candidate_selected is not None:
                    selected_id = str(candidate_selected)
                elif selected_id is not None:
                    if candidate_selected is None:
                        raise ValueError("Obsidian candidate set has no selected target")
                    if selected_id != str(candidate_selected):
                        raise ValueError("selected target does not match the candidate set selection")
            selected = (
                _binding_by_id(conn, owner, selected_id, vault_id=vault) if selected_id is not None else None
            )
            if selected_id is not None and selected is None:
                raise ValueError("selected Obsidian note binding not found")
            if set_id is not None and selected_id is not None:
                assert selected is not None
                selected_item = conn.execute(
                    """SELECT 1 FROM obsidian_candidate_set_items
                       WHERE user_id=? AND candidate_set_id=? AND binding_id=?
                         AND observed_revision=? AND observed_path=?""",
                    (
                        owner,
                        set_id,
                        selected_id,
                        str(selected["current_revision"]),
                        str(selected["current_path"]),
                    ),
                ).fetchone()
                if selected_item is None:
                    raise ValueError("selected target is not current in the candidate set")
            if operation_id is not None:
                operation = conn.execute(
                    """SELECT 1 FROM obsidian_operations
                       WHERE user_id=? AND id=? AND vault_id=?""",
                    (owner, operation_id, vault),
                ).fetchone()
                if operation is None:
                    raise ValueError("last Obsidian operation not found")
            existing = conn.execute(
                "SELECT * FROM obsidian_active_frames WHERE user_id=? AND id=?",
                (owner, opaque_id),
            ).fetchone()
            if existing is None and work_item is not None:
                collision = conn.execute(
                    """SELECT 1 FROM obsidian_active_frames
                       WHERE user_id=? AND work_item_id=?""",
                    (owner, work_item),
                ).fetchone()
                if collision is not None:
                    raise ValueError("work_item_id already belongs to another Obsidian active frame")
            created_at = str(existing["created_at"]) if existing is not None else current
            conn.execute(
                """INSERT INTO obsidian_active_frames(
                       id, user_id, vault_id, work_item_id, active_binding_id,
                       active_path, active_revision, active_heading,
                       candidate_set_id, selected_binding_id, selected_path,
                       selected_revision, last_operation_id, frame_json, state,
                       created_at, expires_at, invalidated_at, updated_at
                   ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, NULL, ?)
                   ON CONFLICT(user_id, id) DO UPDATE SET
                       vault_id=excluded.vault_id,
                       work_item_id=excluded.work_item_id,
                       active_binding_id=excluded.active_binding_id,
                       active_path=excluded.active_path,
                       active_revision=excluded.active_revision,
                       active_heading=excluded.active_heading,
                       candidate_set_id=excluded.candidate_set_id,
                       selected_binding_id=excluded.selected_binding_id,
                       selected_path=excluded.selected_path,
                       selected_revision=excluded.selected_revision,
                       last_operation_id=excluded.last_operation_id,
                       frame_json=excluded.frame_json,
                       state='active', expires_at=excluded.expires_at,
                       invalidated_at=NULL, updated_at=excluded.updated_at""",
                (
                    opaque_id,
                    owner,
                    vault,
                    work_item,
                    active_id,
                    str(active["current_path"]) if active is not None else None,
                    str(active["current_revision"]) if active is not None else None,
                    heading,
                    set_id,
                    selected_id,
                    str(selected["current_path"]) if selected is not None else None,
                    str(selected["current_revision"]) if selected is not None else None,
                    operation_id,
                    frame_json,
                    created_at,
                    expiry,
                    current,
                ),
            )
            row = conn.execute(
                "SELECT * FROM obsidian_active_frames WHERE user_id=? AND id=?",
                (owner, opaque_id),
            ).fetchone()
            assert row is not None
            return dict(row)

    def get_obsidian_active_frame(
        self,
        user_id: str,
        frame_id: str | None = None,
        *,
        work_item_id: str | None = None,
        now: str | None = None,
        include_inactive: bool = False,
    ) -> dict[str, Any] | None:
        owner = validate_user_id(user_id)
        if (frame_id is None) == (work_item_id is None):
            raise ValueError("provide exactly one of frame_id or work_item_id")
        field = "id" if frame_id is not None else "work_item_id"
        identifier = _bounded_text(
            frame_id if frame_id is not None else work_item_id,
            field,
            maximum=200,
        )
        current = _canonical_timestamp(now or utc_now(), "now")
        with self.transaction() as conn:
            row = conn.execute(
                f"SELECT * FROM obsidian_active_frames WHERE user_id=? AND {field}=?",  # nosec B608
                (owner, identifier),
            ).fetchone()
            if row is None:
                return None
            status = str(row["state"])
            if status == "active" and str(row["expires_at"]) <= current:
                conn.execute(
                    """UPDATE obsidian_active_frames
                       SET state='expired', invalidated_at=?, updated_at=?
                       WHERE user_id=? AND id=?""",
                    (current, current, owner, str(row["id"])),
                )
                status = "expired"
            if status == "active":
                stale = conn.execute(
                    """SELECT 1
                       WHERE EXISTS (
                           SELECT 1 WHERE ? IS NOT NULL AND NOT EXISTS (
                               SELECT 1 FROM obsidian_note_bindings binding
                                WHERE binding.user_id=? AND binding.id=?
                                  AND binding.vault_id=? AND binding.deleted_at IS NULL
                                  AND binding.current_path=? AND binding.current_revision=?
                           )
                       ) OR EXISTS (
                           SELECT 1 WHERE ? IS NOT NULL AND NOT EXISTS (
                               SELECT 1 FROM obsidian_note_bindings binding
                                WHERE binding.user_id=? AND binding.id=?
                                  AND binding.vault_id=? AND binding.deleted_at IS NULL
                                  AND binding.current_path=? AND binding.current_revision=?
                           )
                       ) OR EXISTS (
                           SELECT 1 WHERE ? IS NOT NULL AND NOT EXISTS (
                               SELECT 1 FROM obsidian_candidate_sets candidate_set
                                WHERE candidate_set.user_id=? AND candidate_set.id=?
                                  AND candidate_set.vault_id=?
                                  AND candidate_set.status IN ('active', 'selected')
                                  AND candidate_set.expires_at>?
                                  AND candidate_set.selected_binding_id IS ?
                           )
                       )""",
                    (
                        row["active_binding_id"],
                        owner,
                        row["active_binding_id"],
                        row["vault_id"],
                        row["active_path"],
                        row["active_revision"],
                        row["selected_binding_id"],
                        owner,
                        row["selected_binding_id"],
                        row["vault_id"],
                        row["selected_path"],
                        row["selected_revision"],
                        row["candidate_set_id"],
                        owner,
                        row["candidate_set_id"],
                        row["vault_id"],
                        current,
                        row["selected_binding_id"],
                    ),
                ).fetchone()
                if stale is not None:
                    conn.execute(
                        """UPDATE obsidian_active_frames
                           SET state='invalidated', invalidated_at=?, updated_at=?
                           WHERE user_id=? AND id=?""",
                        (current, current, owner, str(row["id"])),
                    )
                    status = "invalidated"
            if status != "active" and not include_inactive:
                return None
            updated = conn.execute(
                "SELECT * FROM obsidian_active_frames WHERE user_id=? AND id=?",
                (owner, str(row["id"])),
            ).fetchone()
            assert updated is not None
            return dict(updated)

    def invalidate_obsidian_active_frame(
        self,
        user_id: str,
        frame_id: str | None = None,
        *,
        work_item_id: str | None = None,
    ) -> dict[str, Any] | None:
        owner = validate_user_id(user_id)
        if (frame_id is None) == (work_item_id is None):
            raise ValueError("provide exactly one of frame_id or work_item_id")
        field = "id" if frame_id is not None else "work_item_id"
        identifier = _bounded_text(
            frame_id if frame_id is not None else work_item_id,
            field,
            maximum=200,
        )
        with self.transaction() as conn:
            row = conn.execute(
                f"SELECT * FROM obsidian_active_frames WHERE user_id=? AND {field}=?",  # nosec B608
                (owner, identifier),
            ).fetchone()
            if row is None:
                return None
            if str(row["state"]) == "active":
                now = utc_now()
                conn.execute(
                    """UPDATE obsidian_active_frames
                       SET state='invalidated', invalidated_at=?, updated_at=?
                       WHERE user_id=? AND id=?""",
                    (now, now, owner, str(row["id"])),
                )
            updated = conn.execute(
                "SELECT * FROM obsidian_active_frames WHERE user_id=? AND id=?",
                (owner, str(row["id"])),
            ).fetchone()
            assert updated is not None
            return dict(updated)

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

    def get_obsidian_conflict(
        self,
        user_id: str,
        conflict_id: str,
    ) -> dict[str, Any] | None:
        owner = validate_user_id(user_id)
        opaque_id = _bounded_text(conflict_id, "conflict_id", maximum=200)
        row = self.execute(
            "SELECT * FROM obsidian_conflicts WHERE user_id=? AND id=?",
            (owner, opaque_id),
        ).fetchone()
        return None if row is None else dict(row)

    def resolve_obsidian_conflict(
        self,
        user_id: str,
        conflict_id: str,
        *,
        vault_id: str,
        canonical_path: str,
        conflict_path: str,
        canonical_revision: str,
        conflict_revision: str,
        merged_revision: str,
        operation_id: str,
    ) -> dict[str, Any]:
        """Resolve one exact conflict only after its durable merge receipt exists."""

        owner = validate_user_id(user_id)
        opaque_id = _bounded_text(conflict_id, "conflict_id", maximum=200)
        vault = _bounded_text(vault_id, "vault_id", maximum=200)
        canonical = _note_path(canonical_path, "canonical_path")
        artifact = _note_path(conflict_path, "conflict_path")
        if ".sync-conflict-" in canonical.rsplit("/", 1)[-1].casefold():
            raise ValueError("canonical_path cannot name a conflict artifact")
        if ".sync-conflict-" not in artifact.rsplit("/", 1)[-1].casefold():
            raise ValueError("conflict_path must name a conflict artifact")
        before = _revision(canonical_revision, "canonical_revision")
        conflict_before = _revision(conflict_revision, "conflict_revision")
        merged = _revision(merged_revision, "merged_revision")
        operation = _bounded_text(operation_id, "operation_id", maximum=200)
        resolution = {
            "schema": "friday.obsidian-conflict-resolution.v1",
            "strategy": "preserve_both",
            "conflict_id": opaque_id,
            "canonical_path": canonical,
            "conflict_path": artifact,
            "canonical_revision": before,
            "conflict_revision": conflict_before,
            "merged_revision": merged,
            "operation_id": operation,
        }
        encoded_resolution = _json_object(resolution, "resolution")
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM obsidian_conflicts WHERE user_id=? AND id=?",
                (owner, opaque_id),
            ).fetchone()
            if row is None:
                raise ValueError("Obsidian conflict not found")
            if (
                str(row["vault_id"]) != vault
                or str(row["canonical_path"]) != canonical
                or str(row["conflict_path"]) != artifact
            ):
                raise ValueError("Obsidian conflict identity changed")
            durable = conn.execute(
                "SELECT * FROM obsidian_operations WHERE user_id=? AND id=? AND vault_id=?",
                (owner, operation, vault),
            ).fetchone()
            if durable is None:
                raise ValueError("durable conflict merge operation not found")
            if (
                str(durable["method"]) != "conflict_merge"
                or str(durable["expected_revision"] or "") != before
                or str(durable["status"])
                not in {
                    "committed",
                    "scan_pending",
                    "scan_complete",
                    "delivery_pending",
                    "delivered",
                    "reconciled",
                }
            ):
                raise ValueError("durable conflict merge operation is not successful")
            try:
                durable_result = json.loads(str(durable["result_json"]))
            except json.JSONDecodeError as exc:
                raise ValueError("durable conflict merge receipt is invalid") from exc
            if (
                not isinstance(durable_result, Mapping)
                or durable_result.get("schema") != "friday.obsidian-note-operation.v1"
                or durable_result.get("path") != canonical
                or durable_result.get("revision") != merged
                or durable_result.get("previous_revision") != before
                or durable_result.get("target_revision") != merged
                or durable_result.get("conflict_id") != opaque_id
                or durable_result.get("conflict_path") != artifact
                or durable_result.get("conflict_revision") != conflict_before
            ):
                raise ValueError("durable conflict merge receipt does not match the resolution")
            status = str(row["status"])
            if status == "resolved":
                try:
                    previous = json.loads(str(row["resolution_json"] or "{}"))
                except json.JSONDecodeError as exc:
                    raise ValueError("stored Obsidian conflict resolution is invalid") from exc
                if previous != resolution:
                    raise ValueError("Obsidian conflict was resolved by a different operation")
            elif status == "open":
                conn.execute(
                    """UPDATE obsidian_conflicts
                       SET status='resolved', resolution_json=?, updated_at=?
                       WHERE user_id=? AND id=? AND status='open'""",
                    (encoded_resolution, utc_now(), owner, opaque_id),
                )
            else:
                raise ValueError("Obsidian conflict is not open")
            updated = conn.execute(
                "SELECT * FROM obsidian_conflicts WHERE user_id=? AND id=?",
                (owner, opaque_id),
            ).fetchone()
            assert updated is not None
            return dict(updated)


__all__ = [
    "OBSIDIAN_SCHEMA",
    "ObsidianMixin",
    "upgrade_obsidian_schema_35_to_36",
    "validate_obsidian_schema",
]
