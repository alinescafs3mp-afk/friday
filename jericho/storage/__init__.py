"""SQLite persistence for Jericho.

The storage layer is intentionally explicit and tenant-aware.  Every mutable
knowledge record belongs to a user, all deletes are soft by default, and merge
or update operations leave version/audit evidence behind.  SQLite's online
backup API is exposed so a consistent backup can be produced while Jericho is
running.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import math
import os
import re
import shutil
import sqlite3
import tempfile
import threading
import time
import unicodedata
from collections.abc import Iterator, Sequence
from contextlib import contextmanager, suppress
from datetime import UTC, datetime, timedelta
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from jericho.config import JerichoSettings
from jericho.storage.models import (
    AuditEntry,
    Entity,
    EntityResolutionCandidate,
    EntityType,
    FeedbackItem,
    InboxItem,
    InboxStatus,
    KnowledgeObject,
    LifecycleStage,
    Mission,
    MissionStatus,
    MissionTask,
    RawObject,
    Relation,
    RelationType,
    ResolutionStatus,
    enum_value,
    new_id,
    utc_now,
)

LOGGER = logging.getLogger(__name__)
SCHEMA_VERSION = 15


class UnsupportedSchemaVersionError(RuntimeError):
    """The database was created by an incompatible newer or corrupt schema."""


class SourceReferenceConflictError(ValueError):
    """An immutable source reference was reused for different content."""


_USER_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,199}$")
CONVERSATION_MODES = {"dialogue", "knowledge_work", "research"}


def validate_user_id(user_id: str) -> str:
    """Validate stable tenant identifiers before they reach SQL or UI routes."""
    value = str(user_id or "").strip()
    if not _USER_ID_RE.fullmatch(value):
        raise ValueError(
            "user_id must be 1-200 characters using letters, digits, dot, underscore, colon, @, +, or -"
        )
    return value


def normalize_conversation_mode(mode: str | None) -> str:
    value = str(mode or "dialogue").strip().casefold().replace("-", "_")
    aliases = {"chat": "dialogue", "work": "knowledge_work", "knowledge": "knowledge_work"}
    value = aliases.get(value, value)
    if value not in CONVERSATION_MODES:
        raise ValueError("mode must be dialogue, knowledge_work, or research")
    return value


_JSON_COLUMNS = {
    "metadata_json",
    "tags_json",
    "aliases_json",
    "suggested_tags_json",
    "suggestions_json",
    "evidence_json",
    "context_json",
    "before_json",
    "after_json",
    "snapshot_json",
    "depends_on_json",
    "tools_used_json",
}

CORE_SCHEMA = """
PRAGMA foreign_keys=ON;
PRAGMA busy_timeout=5000;

CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL DEFAULT 'local',
    external_id TEXT NOT NULL DEFAULT '',
    display_name TEXT NOT NULL DEFAULT '',
    username TEXT NOT NULL DEFAULT '',
    preset_key TEXT NOT NULL DEFAULT 'user',
    status TEXT NOT NULL DEFAULT 'active',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS custom_presets (
    preset_key TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS preset_capabilities (
    preset_key TEXT NOT NULL,
    security_id TEXT NOT NULL,
    PRIMARY KEY (preset_key, security_id)
);

CREATE TABLE IF NOT EXISTS user_permission_overrides (
    user_id TEXT NOT NULL REFERENCES users(id),
    security_id TEXT NOT NULL,
    effect TEXT NOT NULL CHECK(effect IN ('allow', 'deny')),
    updated_at TEXT NOT NULL,
    PRIMARY KEY (user_id, security_id)
);

CREATE TABLE IF NOT EXISTS raw_objects (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id),
    source TEXT NOT NULL,
    source_ref TEXT NOT NULL DEFAULT '',
    raw_content TEXT NOT NULL DEFAULT '',
    content_type TEXT NOT NULL DEFAULT 'text',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    content_hash TEXT NOT NULL DEFAULT '',
    version INTEGER NOT NULL DEFAULT 1,
    received_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    deleted_at TEXT
);

CREATE TABLE IF NOT EXISTS knowledge_objects (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id),
    raw_object_id TEXT NOT NULL REFERENCES raw_objects(id),
    entity_id TEXT,
    content TEXT NOT NULL DEFAULT '',
    content_type TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL DEFAULT '',
    tags_json TEXT NOT NULL DEFAULT '[]',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    knowledge_kind TEXT NOT NULL DEFAULT 'note',
    importance REAL NOT NULL DEFAULT 0.5 CHECK(importance >= 0 AND importance <= 1),
    quality_score REAL NOT NULL DEFAULT 0.5 CHECK(quality_score >= 0 AND quality_score <= 1),
    promotion_score REAL NOT NULL DEFAULT 0.5 CHECK(promotion_score >= 0 AND promotion_score <= 1),
    lifecycle_stage TEXT NOT NULL DEFAULT 'active',
    version INTEGER NOT NULL DEFAULT 1,
    superseded_by_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    deleted_at TEXT
);

CREATE TABLE IF NOT EXISTS knowledge_object_versions (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id),
    knowledge_object_id TEXT NOT NULL REFERENCES knowledge_objects(id),
    version INTEGER NOT NULL,
    snapshot_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(knowledge_object_id, version)
);

CREATE TABLE IF NOT EXISTS inbox (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id),
    raw_object_id TEXT NOT NULL REFERENCES raw_objects(id),
    knowledge_object_id TEXT REFERENCES knowledge_objects(id),
    status TEXT NOT NULL DEFAULT 'pending',
    suggested_entity_id TEXT,
    suggested_tags_json TEXT NOT NULL DEFAULT '[]',
    suggestions_json TEXT NOT NULL DEFAULT '{}',
    suggested_action TEXT NOT NULL DEFAULT 'review',
    promotion_score REAL NOT NULL DEFAULT 0.0 CHECK(promotion_score >= 0 AND promotion_score <= 1),
    quality_score REAL NOT NULL DEFAULT 0.0 CHECK(quality_score >= 0 AND quality_score <= 1),
    classification_notes TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    reviewed_at TEXT,
    reviewed_by TEXT
);

CREATE TABLE IF NOT EXISTS entities (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id),
    name TEXT NOT NULL,
    normalized_name TEXT NOT NULL DEFAULT '',
    entity_type TEXT NOT NULL DEFAULT 'other',
    aliases_json TEXT NOT NULL DEFAULT '[]',
    description TEXT NOT NULL DEFAULT '',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    canonical INTEGER NOT NULL DEFAULT 1,
    merged_into_id TEXT,
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    deleted_at TEXT
);

CREATE TABLE IF NOT EXISTS entity_versions (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id),
    entity_id TEXT NOT NULL REFERENCES entities(id),
    version INTEGER NOT NULL,
    snapshot_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(entity_id, version)
);

CREATE TABLE IF NOT EXISTS knowledge_entity_links (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id),
    knowledge_object_id TEXT NOT NULL REFERENCES knowledge_objects(id),
    entity_id TEXT NOT NULL REFERENCES entities(id),
    status TEXT NOT NULL DEFAULT 'accepted',
    confidence REAL NOT NULL DEFAULT 1.0,
    evidence_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    reviewed_at TEXT,
    reviewed_by TEXT,
    UNIQUE(user_id, knowledge_object_id, entity_id)
);

CREATE TABLE IF NOT EXISTS relations (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id),
    source_entity_id TEXT NOT NULL REFERENCES entities(id),
    target_entity_id TEXT NOT NULL REFERENCES entities(id),
    relation_type TEXT NOT NULL DEFAULT 'related_to',
    weight REAL NOT NULL DEFAULT 1.0,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    deleted_at TEXT,
    CHECK(source_entity_id <> target_entity_id)
);

CREATE TABLE IF NOT EXISTS entity_resolution_candidates (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id),
    entity_a_id TEXT NOT NULL REFERENCES entities(id),
    entity_b_id TEXT NOT NULL REFERENCES entities(id),
    pair_key TEXT NOT NULL,
    confidence REAL NOT NULL DEFAULT 0.0,
    resolution_method TEXT NOT NULL DEFAULT 'name_similarity',
    evidence_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'suggested',
    resolved_by TEXT,
    created_at TEXT NOT NULL,
    resolved_at TEXT,
    UNIQUE(user_id, pair_key),
    CHECK(entity_a_id <> entity_b_id)
);

CREATE TABLE IF NOT EXISTS entity_merge_history (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id),
    source_entity_id TEXT NOT NULL,
    target_entity_id TEXT NOT NULL,
    source_snapshot_json TEXT NOT NULL,
    target_before_json TEXT NOT NULL,
    target_after_json TEXT NOT NULL,
    merged_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS feedback (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id),
    target_type TEXT NOT NULL,
    target_id TEXT NOT NULL,
    feedback_type TEXT NOT NULL DEFAULT 'general',
    score REAL NOT NULL DEFAULT 0.0 CHECK(score >= -1 AND score <= 1),
    comment TEXT NOT NULL DEFAULT '',
    context_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS feedback_state (
    user_id TEXT NOT NULL REFERENCES users(id),
    target_type TEXT NOT NULL,
    target_id TEXT NOT NULL,
    feedback_type TEXT NOT NULL,
    score REAL NOT NULL DEFAULT 0.0 CHECK(score >= -1 AND score <= 1),
    comment TEXT NOT NULL DEFAULT '',
    context_json TEXT NOT NULL DEFAULT '{}',
    feedback_id TEXT NOT NULL REFERENCES feedback(id),
    updated_at TEXT NOT NULL,
    PRIMARY KEY(user_id, target_type, target_id, feedback_type)
);

CREATE TABLE IF NOT EXISTS knowledge_usage (
    user_id TEXT NOT NULL REFERENCES users(id),
    knowledge_object_id TEXT NOT NULL REFERENCES knowledge_objects(id),
    retrieval_count INTEGER NOT NULL DEFAULT 0,
    answer_count INTEGER NOT NULL DEFAULT 0,
    positive_feedback_count INTEGER NOT NULL DEFAULT 0,
    negative_feedback_count INTEGER NOT NULL DEFAULT 0,
    last_retrieved_at TEXT,
    last_used_at TEXT,
    last_feedback_at TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(user_id, knowledge_object_id)
);

CREATE TABLE IF NOT EXISTS relation_candidates (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id),
    source_entity_id TEXT NOT NULL REFERENCES entities(id),
    target_entity_id TEXT NOT NULL REFERENCES entities(id),
    relation_type TEXT NOT NULL DEFAULT 'related_to',
    confidence REAL NOT NULL DEFAULT 0.0 CHECK(confidence >= 0 AND confidence <= 1),
    evidence_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'suggested' CHECK(status IN ('suggested', 'accepted', 'rejected')),
    created_at TEXT NOT NULL,
    reviewed_at TEXT,
    reviewed_by TEXT,
    UNIQUE(user_id, source_entity_id, target_entity_id, relation_type),
    CHECK(source_entity_id <> target_entity_id)
);

CREATE TABLE IF NOT EXISTS knowledge_conflicts (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id),
    knowledge_a_id TEXT NOT NULL REFERENCES knowledge_objects(id),
    knowledge_b_id TEXT NOT NULL REFERENCES knowledge_objects(id),
    pair_key TEXT NOT NULL,
    conflict_type TEXT NOT NULL DEFAULT 'potential_contradiction',
    confidence REAL NOT NULL DEFAULT 0.0 CHECK(confidence >= 0 AND confidence <= 1),
    evidence_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'suggested' CHECK(status IN ('suggested', 'confirmed', 'dismissed', 'resolved')),
    created_at TEXT NOT NULL,
    reviewed_at TEXT,
    reviewed_by TEXT,
    resolution_note TEXT NOT NULL DEFAULT '',
    UNIQUE(user_id, pair_key, conflict_type),
    CHECK(knowledge_a_id <> knowledge_b_id)
);

CREATE TABLE IF NOT EXISTS audit_log (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    action TEXT NOT NULL,
    target_type TEXT NOT NULL,
    target_id TEXT,
    before_json TEXT,
    after_json TEXT,
    ip_address TEXT NOT NULL DEFAULT '',
    request_id TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

-- Append-only is enforced at the database level, not by convention: any code
-- path (including future bugs) attempting to rewrite history aborts.
CREATE TRIGGER IF NOT EXISTS audit_log_no_update BEFORE UPDATE ON audit_log
BEGIN
    SELECT RAISE(ABORT, 'audit_log is append-only');
END;

CREATE TRIGGER IF NOT EXISTS audit_log_no_delete BEFORE DELETE ON audit_log
BEGIN
    SELECT RAISE(ABORT, 'audit_log is append-only');
END;

CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id),
    title TEXT NOT NULL DEFAULT '',
    last_message TEXT NOT NULL DEFAULT '',
    unread_count INTEGER NOT NULL DEFAULT 0,
    is_pinned INTEGER NOT NULL DEFAULT 0,
    is_archived INTEGER NOT NULL DEFAULT 0,
    mode TEXT NOT NULL DEFAULT 'dialogue',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations(id),
    user_id TEXT NOT NULL REFERENCES users(id),
    role TEXT NOT NULL,
    content TEXT NOT NULL DEFAULT '',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    reply_to TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS channel_sessions (
    user_id TEXT NOT NULL REFERENCES users(id),
    channel TEXT NOT NULL,
    channel_id TEXT NOT NULL,
    conversation_id TEXT NOT NULL REFERENCES conversations(id),
    mode TEXT NOT NULL DEFAULT 'dialogue',
    updated_at TEXT NOT NULL,
    PRIMARY KEY(user_id, channel, channel_id)
);

CREATE TABLE IF NOT EXISTS runtime_kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS request_idempotency (
    user_id TEXT NOT NULL REFERENCES users(id),
    request_key TEXT NOT NULL,
    request_hash TEXT NOT NULL DEFAULT '',
    response_json TEXT NOT NULL DEFAULT '{}',
    state TEXT NOT NULL DEFAULT 'complete' CHECK(state IN ('pending', 'complete')),
    lease_token TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT '',
    PRIMARY KEY(user_id, request_key)
);

CREATE TABLE IF NOT EXISTS runtime_events (
    id TEXT PRIMARY KEY,
    event_type TEXT NOT NULL,
    payload TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS missions (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id),
    goal TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'ready'
        CHECK(status IN ('proposed', 'ready', 'running', 'paused', 'blocked',
                         'completed', 'failed', 'cancelled')),
    origin TEXT NOT NULL DEFAULT 'user' CHECK(origin IN ('user', 'agent', 'worker')),
    plan_summary TEXT NOT NULL DEFAULT '',
    created_by TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT '',
    task_count INTEGER NOT NULL DEFAULT 0,
    done_count INTEGER NOT NULL DEFAULT 0,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT
);

CREATE TABLE IF NOT EXISTS mission_tasks (
    id TEXT PRIMARY KEY,
    mission_id TEXT NOT NULL REFERENCES missions(id),
    user_id TEXT NOT NULL REFERENCES users(id),
    seq INTEGER NOT NULL,
    kind TEXT NOT NULL DEFAULT 'gather' CHECK(kind IN ('gather', 'produce')),
    title TEXT NOT NULL DEFAULT '',
    instruction TEXT NOT NULL,
    depends_on_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK(status IN ('pending', 'running', 'done', 'failed', 'skipped')),
    result TEXT NOT NULL DEFAULT '',
    inbox_id TEXT,
    tools_used_json TEXT NOT NULL DEFAULT '[]',
    error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT,
    UNIQUE(mission_id, seq)
);

CREATE TABLE IF NOT EXISTS entity_time (
    entity_id TEXT PRIMARY KEY REFERENCES entities(id),
    user_id TEXT NOT NULL REFERENCES users(id),
    occurred_at TEXT NOT NULL,
    occurred_end TEXT,
    precision TEXT NOT NULL DEFAULT 'day',
    source TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS api_tokens (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id),
    token_sha256 TEXT NOT NULL UNIQUE,
    label TEXT NOT NULL DEFAULT '',
    created_by TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    last_used_at TEXT,
    revoked_at TEXT,
    expires_at TEXT
);

CREATE TABLE IF NOT EXISTS knowledge_embeddings (
    knowledge_object_id TEXT PRIMARY KEY REFERENCES knowledge_objects(id),
    user_id TEXT NOT NULL REFERENCES users(id),
    model TEXT NOT NULL,
    dim INTEGER NOT NULL,
    source_version INTEGER NOT NULL DEFAULT 0,
    content_hash TEXT NOT NULL DEFAULT '',
    vector BLOB NOT NULL,
    updated_at TEXT NOT NULL
);

-- Outbound push queue: organs enqueue a message for a target chat; the
-- Telegram bridge drains it (the backend never sends to Telegram itself).
-- ``dedup_key`` makes enqueue idempotent so a periodic organ can re-run safely.
CREATE TABLE IF NOT EXISTS outbound_notifications (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id),
    chat_id TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT '',
    dedup_key TEXT NOT NULL DEFAULT '',
    body TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    sent_at TEXT
);

-- Retrieval eval gold set: a query paired with the Knowledge Objects that a
-- good search must surface. Measured periodically to catch quality regressions.
CREATE TABLE IF NOT EXISTS eval_cases (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id),
    query TEXT NOT NULL,
    expected_ids_json TEXT NOT NULL DEFAULT '[]',
    note TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT 'manual',
    created_at TEXT NOT NULL,
    UNIQUE(user_id, query)
);

CREATE INDEX IF NOT EXISTS idx_users_source_external ON users(source, external_id);
CREATE INDEX IF NOT EXISTS idx_raw_objects_user_received ON raw_objects(user_id, received_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS uq_raw_source_ref
    ON raw_objects(user_id, source, source_ref) WHERE source_ref <> '';
CREATE INDEX IF NOT EXISTS idx_knowledge_user_lifecycle
    ON knowledge_objects(user_id, lifecycle_stage, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_knowledge_user_quality
    ON knowledge_objects(user_id, quality_score DESC, promotion_score DESC, importance DESC);
CREATE INDEX IF NOT EXISTS idx_knowledge_raw ON knowledge_objects(raw_object_id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_knowledge_version
    ON knowledge_object_versions(knowledge_object_id, version);
CREATE INDEX IF NOT EXISTS idx_inbox_user_status ON inbox(user_id, status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_inbox_user_action
    ON inbox(user_id, suggested_action, promotion_score DESC, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_entities_user_type ON entities(user_id, entity_type, normalized_name);
CREATE INDEX IF NOT EXISTS idx_links_entity ON knowledge_entity_links(user_id, entity_id, status);
CREATE INDEX IF NOT EXISTS idx_links_knowledge ON knowledge_entity_links(user_id, knowledge_object_id, status);
CREATE INDEX IF NOT EXISTS idx_relations_source ON relations(user_id, source_entity_id);
CREATE INDEX IF NOT EXISTS idx_relations_target ON relations(user_id, target_entity_id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_active_relation
    ON relations(user_id, source_entity_id, target_entity_id, relation_type)
    WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_resolution_status
    ON entity_resolution_candidates(user_id, status, confidence DESC);
CREATE INDEX IF NOT EXISTS idx_feedback_target ON feedback(user_id, target_type, target_id);
CREATE INDEX IF NOT EXISTS idx_feedback_state_target
    ON feedback_state(user_id, target_type, target_id, feedback_type);
CREATE INDEX IF NOT EXISTS idx_knowledge_usage_rank
    ON knowledge_usage(user_id, answer_count DESC, retrieval_count DESC, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_relation_candidates_status
    ON relation_candidates(user_id, status, confidence DESC, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_knowledge_conflicts_status
    ON knowledge_conflicts(user_id, status, confidence DESC, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_messages_conversation ON messages(user_id, conversation_id, created_at);
CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_log(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_user ON audit_log(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_missions_user_status
    ON missions(user_id, status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_missions_status ON missions(status, created_at);
CREATE INDEX IF NOT EXISTS idx_mission_tasks_mission ON mission_tasks(mission_id, seq);
CREATE INDEX IF NOT EXISTS idx_mission_tasks_status ON mission_tasks(user_id, status);
CREATE INDEX IF NOT EXISTS idx_knowledge_embeddings_user_model
    ON knowledge_embeddings(user_id, model);
CREATE INDEX IF NOT EXISTS idx_api_tokens_user ON api_tokens(user_id, revoked_at);
CREATE INDEX IF NOT EXISTS idx_entity_time_user ON entity_time(user_id, occurred_at);
CREATE INDEX IF NOT EXISTS idx_outbound_status ON outbound_notifications(status, created_at);
CREATE UNIQUE INDEX IF NOT EXISTS uq_outbound_dedup
    ON outbound_notifications(user_id, dedup_key) WHERE dedup_key <> '';
"""

CORE_INDEX_MARKER = "CREATE INDEX IF NOT EXISTS idx_users_source_external"
CORE_TABLE_SCHEMA, CORE_INDEX_SCHEMA_TAIL = CORE_SCHEMA.split(CORE_INDEX_MARKER, 1)
CORE_INDEX_SCHEMA = CORE_INDEX_MARKER + CORE_INDEX_SCHEMA_TAIL

FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_fts USING fts5(
    content,
    title,
    summary,
    tags_json,
    content=knowledge_objects,
    content_rowid=rowid,
    tokenize='unicode61 remove_diacritics 2'
);

CREATE TRIGGER IF NOT EXISTS knowledge_objects_ai AFTER INSERT ON knowledge_objects BEGIN
    INSERT INTO knowledge_fts(rowid, content, title, summary, tags_json)
    VALUES (new.rowid, new.content, new.title, new.summary, new.tags_json);
END;

CREATE TRIGGER IF NOT EXISTS knowledge_objects_ad AFTER DELETE ON knowledge_objects BEGIN
    INSERT INTO knowledge_fts(knowledge_fts, rowid, content, title, summary, tags_json)
    VALUES ('delete', old.rowid, old.content, old.title, old.summary, old.tags_json);
END;

CREATE TRIGGER IF NOT EXISTS knowledge_objects_au AFTER UPDATE ON knowledge_objects BEGIN
    INSERT INTO knowledge_fts(knowledge_fts, rowid, content, title, summary, tags_json)
    VALUES ('delete', old.rowid, old.content, old.title, old.summary, old.tags_json);
    INSERT INTO knowledge_fts(rowid, content, title, summary, tags_json)
    VALUES (new.rowid, new.content, new.title, new.summary, new.tags_json);
END;
"""


_ENTITY_IDENTIFIER_RE = re.compile(r"^[A-Za-zА-ЯЁа-яё0-9][A-Za-zА-ЯЁа-яё0-9._+#/@:-]{1,63}$")


def _is_entity_identifier(value: str) -> bool:
    """Return True for compact codes whose punctuation is semantically significant."""

    compact = unicodedata.normalize("NFKC", value or "").strip()
    if not _ENTITY_IDENTIFIER_RE.fullmatch(compact) or " " in compact:
        return False
    return bool(
        re.search(r"[0-9._+#/@:-]", compact)
        or (len(compact) <= 16 and compact.upper() == compact and re.search(r"[A-ZА-ЯЁ]", compact))
    )


def normalize_entity_name(value: str) -> str:
    """Normalize entity names while preserving exact identifiers and contract codes.

    Human names are whitespace/punctuation normalized for useful alias lookup.  Compact
    identifiers keep dots, slashes, dashes, and suffixes so distinct symbols do not collapse.
    """

    raw = unicodedata.normalize("NFKC", value or "").strip()
    if _is_entity_identifier(raw):
        return raw.casefold()
    text = raw.casefold()
    text = re.sub(r"[\s\-_./]+", " ", text)
    text = re.sub(r"[^\w\s#+&]", "", text, flags=re.UNICODE)
    return " ".join(text.split())


def _json_load(value: Any, default: Any) -> Any:
    if value in (None, ""):
        return default
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default


def _snapshot(row: dict[str, Any]) -> str:
    return json.dumps(row, ensure_ascii=False, sort_keys=True, default=str)


def _safe_filename(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9_.-]+", "-", value.strip())
    return value.strip(".-")[:80] or "backup"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _chmod_private(path: Path) -> None:
    """Best-effort owner-only permissions; Windows ACLs remain operator-managed."""
    try:
        path.chmod(0o600)
    except OSError:
        LOGGER.warning("Could not restrict permissions on %s", path)


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        _chmod_private(temporary)
        os.replace(temporary, path)
        _chmod_private(path)
    finally:
        temporary.unlink(missing_ok=True)


def _stage_private_copy(source: Path, destination: Path) -> Path:
    """Copy *source* beside *destination* and fsync it before replacement.

    Restore deliberately stages on the same filesystem as the live database so
    the final ``os.replace`` is atomic.  The helper never follows a destination
    symlink and keeps the temporary file owner-only where the platform permits.
    """

    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.restore-",
        suffix=".tmp",
        dir=destination.parent,
    )
    temporary = Path(temporary_name)
    try:
        with source.open("rb") as source_handle, os.fdopen(descriptor, "wb") as target_handle:
            shutil.copyfileobj(source_handle, target_handle, length=1024 * 1024)
            target_handle.flush()
            os.fsync(target_handle.fileno())
        _chmod_private(temporary)
        return temporary
    except BaseException:
        with suppress(OSError):
            os.close(descriptor)
        temporary.unlink(missing_ok=True)
        raise


def _fsync_directory(path: Path) -> None:
    """Best-effort durability barrier for an atomic filesystem replacement."""

    if os.name == "nt":
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _write_recovery_bundle(
    settings: JerichoSettings,
    snapshots: dict[Path, Path],
    *,
    label: str,
    reason_type: str,
) -> dict[str, Any]:
    """Persist an exact, explicitly unverified pre-restore file bundle.

    A corrupt or future-schema active database may be impossible to open with
    SQLite's online-backup API.  Refusing to preserve it would make recovery
    destructive; calling it a verified backup would be misleading.  The raw DB
    and any WAL/SHM sidecars are therefore copied to a separate directory with
    hashes and an ``unverified`` manifest that normal restore discovery ignores.
    """

    settings.backups_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    base = settings.backups_dir / f"recovery-{timestamp}-{_safe_filename(label)}"
    destination = base
    suffix = 1
    while destination.exists():
        destination = Path(f"{base}-{suffix}")
        suffix += 1
    destination.mkdir(mode=0o700, parents=False, exist_ok=False)
    files: list[dict[str, Any]] = []
    try:
        for original, snapshot in snapshots.items():
            target = destination / original.name
            staged = _stage_private_copy(snapshot, target)
            try:
                os.replace(staged, target)
            finally:
                staged.unlink(missing_ok=True)
            _chmod_private(target)
            files.append(
                {
                    "name": target.name,
                    "size_bytes": target.stat().st_size,
                    "sha256": _sha256_file(target),
                }
            )
        manifest = {
            "created_at": utc_now(),
            "label": label,
            "verified": False,
            "restorable_by_automatic_command": False,
            "reason_type": reason_type,
            "files": files,
            "note": (
                "Exact pre-restore SQLite files preserved because a verified online "
                "backup could not be created. Inspect manually before use."
            ),
        }
        manifest_path = destination / "recovery.json"
        _write_json_atomic(manifest_path, manifest)
        _fsync_directory(destination)
        _fsync_directory(destination.parent)
        return {
            **manifest,
            "path": str(destination),
            "manifest_path": str(manifest_path),
        }
    except BaseException:
        shutil.rmtree(destination, ignore_errors=True)
        raise


class JerichoStorage:
    """Thread-safe local SQLite repository with explicit tenant boundaries."""

    def __init__(self, settings: JerichoSettings) -> None:
        self.settings = settings
        self._db_path = settings.database_path
        # Connection-per-thread: no two threads ever share one sqlite3.Connection,
        # so cursors are never stepped concurrently on a single connection (the
        # half-written-read hazard). WAL then gives many concurrent readers plus a
        # single writer across the separate connections. ``threading.local`` holds
        # each thread's connection; the registry lets close() shut every thread's
        # connection down (e.g. before a restore swaps the database file), and the
        # generation counter invalidates the per-thread caches after close() so the
        # next use transparently reopens.
        self._local = threading.local()
        self._registry_lock = threading.Lock()
        self._connections: list[sqlite3.Connection] = []
        self._generation = 0
        # Writers serialise through this process-wide lock, held for the whole
        # transaction() block. Reads never take it, so the WAL many-reader win is
        # preserved; writers, however, keep the single-writer invariant in Python
        # (no two BEGIN IMMEDIATE ever contend at the SQLite level, so a writer can
        # never surface "database is locked" from internal contention). close()
        # also takes it, so a shutdown cannot close a connection out from under an
        # in-flight write transaction on a background-worker thread — restoring the
        # drain-before-close guarantee the old shared RLock provided. Reentrant so
        # nested transaction() on one thread does not self-deadlock.
        self._write_lock = threading.RLock()
        # Schema creation/migration runs exactly once, on the first connection,
        # behind this lock; every other connection only opens and configures itself.
        self._init_lock = threading.Lock()
        self._schema_ready = False
        self._fts_available = True

    @property
    def conn(self) -> sqlite3.Connection:
        local = self._local
        cached = getattr(local, "conn", None)
        if cached is not None and getattr(local, "generation", None) == self._generation:
            return cached
        connection = self._open()
        with self._registry_lock:
            self._connections.append(connection)
            local.conn = connection
            local.generation = self._generation
        return connection

    @staticmethod
    def _is_sqlite_busy(exc: sqlite3.OperationalError) -> bool:
        message = str(exc).lower()
        return "database is locked" in message or "database is busy" in message

    def _open(self) -> sqlite3.Connection:
        """Open and migrate SQLite, tolerating concurrent first-start workers.

        SQLite's connection timeout does not consistently cover
        ``PRAGMA journal_mode=WAL`` or schema initialization.  A second process
        starting at the same instant can therefore receive ``database is
        locked`` before normal busy handling applies.  Retrying the complete,
        idempotent initialization avoids exposing a half-configured connection.
        """

        deadline = time.monotonic() + 15.0
        delay = 0.025
        while True:
            try:
                return self._open_once()
            except sqlite3.OperationalError as exc:
                if not self._is_sqlite_busy(exc) or time.monotonic() >= deadline:
                    raise
                time.sleep(delay)
                delay = min(delay * 1.8, 0.5)

    def _open_once(self) -> sqlite3.Connection:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False so close() can shut every thread's connection
        # down from the shutdown/restore thread; cross-thread *use* is prevented
        # structurally (the conn property only ever hands back the caller thread's
        # own connection via threading.local), not by the sqlite thread check.
        conn = sqlite3.connect(str(self._db_path), check_same_thread=False, timeout=10.0)
        conn.row_factory = sqlite3.Row
        try:
            # Per-connection configuration — applied to EVERY thread's connection.
            # busy_timeout must precede WAL negotiation; sqlite3's connect timeout
            # alone does not reliably protect that PRAGMA on every platform. WAL is
            # persistent in the database header (idempotent to re-issue), but
            # foreign_keys and synchronous are per-connection and non-persistent, so
            # every connection must set them or FK enforcement silently disappears.
            conn.execute("PRAGMA busy_timeout=10000")
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA synchronous=NORMAL")
            # SQLite's lower()/NOCASE only fold ASCII; tags and entity names are
            # frequently Cyrillic, so Unicode-correct case-insensitive queries
            # (browse-by-tag) go through Python's casefold instead. User functions
            # are registered per connection.
            conn.create_function(
                "jericho_casefold",
                1,
                lambda value: str(value).casefold() if value is not None else None,
                deterministic=True,
            )
            # Schema creation/migration/FTS is applied exactly once, by the first
            # connection; later connections open against the already-migrated file.
            self._ensure_schema(conn)
            # The core migration transiently lowers busy_timeout (a PRAGMA embedded
            # in CORE_SCHEMA); restore the uniform value on the migrating connection.
            conn.execute("PRAGMA busy_timeout=10000")
            return conn
        except BaseException:
            conn.close()
            raise

    def _ensure_schema(self, conn: sqlite3.Connection) -> None:
        """Create/upgrade the schema exactly once, behind a barrier.

        The first connection runs the (idempotent) migration; no connection may
        run application queries until it has committed, so the double-checked
        ``_init_lock`` both serialises the single migration and makes later
        openers wait for it. A busy failure propagates so ``_open``'s retry loop
        re-runs the still-idempotent migration on a fresh connection.
        """

        if self._schema_ready:
            return
        with self._init_lock:
            if self._schema_ready:
                return
            self._migrate_schema(conn)
            self._schema_ready = True

    def _migrate_schema(self, conn: sqlite3.Connection) -> None:
        # Fail closed before executing any DDL. Opening a database produced by a
        # newer Jericho build must never rewrite its schema marker or attempt a
        # best-effort downgrade. A malformed explicit marker is treated the same
        # way; databases without a marker remain valid pre-release migrations.
        schema_meta_preexisting = (
            conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_meta'").fetchone()
            is not None
        )
        previous_schema_version: str | None = None
        if schema_meta_preexisting:
            row = conn.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()
            previous_schema_version = str(row[0]).strip() if row else None
            if previous_schema_version:
                try:
                    parsed_version = int(previous_schema_version)
                except ValueError as exc:
                    raise UnsupportedSchemaVersionError(
                        f"Invalid database schema version: {previous_schema_version!r}"
                    ) from exc
                if parsed_version < 0 or parsed_version > SCHEMA_VERSION:
                    raise UnsupportedSchemaVersionError(
                        f"Database schema version {parsed_version} is not supported by "
                        f"this Jericho build (maximum {SCHEMA_VERSION})"
                    )

        fts_preexisting = (
            conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='knowledge_fts'").fetchone()
            is not None
        )
        # Create/recognize tables first, then add legacy columns, and only then
        # create indexes that reference those columns. Keep this core migration in
        # one explicit transaction: sqlite3's executescript() commits an existing
        # transaction implicitly, which otherwise leaves half-applied DDL/backfills
        # after a migration failure.
        try:
            conn.execute("BEGIN IMMEDIATE")
            self._execute_statements(conn, CORE_TABLE_SCHEMA)
            self._migrate_legacy_schema(conn)
            self._execute_statements(conn, CORE_INDEX_SCHEMA)
            conn.execute(
                "INSERT OR REPLACE INTO schema_meta(key, value, updated_at) VALUES('schema_version', ?, ?)",
                (str(SCHEMA_VERSION), utc_now()),
            )
            conn.commit()
        except BaseException:
            if conn.in_transaction:
                conn.rollback()
            raise

        # FTS is an optional, self-healing derivative index. Build it only after
        # the authoritative core schema/data transaction has committed, so an
        # unavailable FTS5 module can never roll back or partially expose personal
        # knowledge migration.
        try:
            conn.executescript(FTS_SCHEMA)
            knowledge_count = conn.execute("SELECT COUNT(*) FROM knowledge_objects").fetchone()[0]
            if knowledge_count and not fts_preexisting:
                # An external-content FTS table created after rows already exist
                # starts with an empty index. Rebuild before update triggers can
                # attempt to delete missing index entries.
                conn.execute("INSERT INTO knowledge_fts(knowledge_fts) VALUES('rebuild')")
            elif fts_preexisting and previous_schema_version != str(SCHEMA_VERSION):
                try:
                    conn.execute("INSERT INTO knowledge_fts(knowledge_fts) VALUES('integrity-check')")
                except sqlite3.DatabaseError:
                    LOGGER.warning("Rebuilding an inconsistent knowledge FTS index")
                    conn.execute("INSERT INTO knowledge_fts(knowledge_fts) VALUES('rebuild')")
        except sqlite3.OperationalError as exc:
            if self._is_sqlite_busy(exc):
                raise
            self._fts_available = False
            LOGGER.warning("SQLite FTS5 is unavailable; using LIKE search: %s", exc)
        conn.commit()

    @staticmethod
    def _execute_statements(conn: sqlite3.Connection, script: str) -> None:
        """Execute a plain SQL script without sqlite3's implicit commit.

        ``sqlite3.complete_statement`` handles multiline indexes, comments and
        trigger bodies (it only reports completion at the trigger's final
        ``END;``) while preserving the caller's explicit transaction.
        """

        pending = ""
        for line in script.splitlines(keepends=True):
            pending += line
            if not sqlite3.complete_statement(pending):
                continue
            statement = pending.strip()
            pending = ""
            if statement:
                conn.execute(statement)
        if pending.strip():
            raise sqlite3.OperationalError("Incomplete SQL statement in core schema")

    @staticmethod
    def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
        return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}

    def _migrate_legacy_schema(self, conn: sqlite3.Connection) -> None:
        """Upgrade pre-release databases without discarding personal data.

        Early Jericho builds shipped before the users table, provenance hashes,
        normalized entity names and JSON snapshots existed. SQLite cannot add
        foreign keys or table constraints in place, so the migration adds the
        columns needed by current code and backfills equivalent records. New
        installations take the same idempotent path with no data to transform.
        """
        table_names = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        additions: dict[str, dict[str, str]] = {
            "raw_objects": {
                "content_hash": "TEXT NOT NULL DEFAULT ''",
                "version": "INTEGER NOT NULL DEFAULT 1",
                "deleted_at": "TEXT",
            },
            "knowledge_object_versions": {
                "user_id": "TEXT NOT NULL DEFAULT ''",
                "snapshot_json": "TEXT NOT NULL DEFAULT '{}'",
            },
            "knowledge_objects": {
                "metadata_json": "TEXT NOT NULL DEFAULT '{}'",
                "knowledge_kind": "TEXT NOT NULL DEFAULT 'note'",
                "quality_score": "REAL NOT NULL DEFAULT 0.5",
                "promotion_score": "REAL NOT NULL DEFAULT 0.5",
            },
            "inbox": {
                "knowledge_object_id": "TEXT",
                "suggestions_json": "TEXT NOT NULL DEFAULT '{}'",
                "suggested_action": "TEXT NOT NULL DEFAULT 'review'",
                "promotion_score": "REAL NOT NULL DEFAULT 0.0",
                "quality_score": "REAL NOT NULL DEFAULT 0.0",
            },
            "entities": {"normalized_name": "TEXT NOT NULL DEFAULT ''"},
            "entity_resolution_candidates": {
                "pair_key": "TEXT NOT NULL DEFAULT ''",
                "evidence_json": "TEXT NOT NULL DEFAULT '{}'",
            },
            "audit_log": {"request_id": "TEXT NOT NULL DEFAULT ''"},
            "request_idempotency": {
                "request_hash": "TEXT NOT NULL DEFAULT ''",
                "state": "TEXT NOT NULL DEFAULT 'complete'",
                "lease_token": "TEXT NOT NULL DEFAULT ''",
                "updated_at": "TEXT NOT NULL DEFAULT ''",
            },
            "conversations": {"mode": "TEXT NOT NULL DEFAULT 'dialogue'"},
            "channel_sessions": {"mode": "TEXT NOT NULL DEFAULT 'dialogue'"},
            # NULL expires_at = a non-expiring token (all legacy tokens stay valid).
            "api_tokens": {"expires_at": "TEXT"},
        }
        for table, columns in additions.items():
            if table not in table_names:
                continue
            existing = self._table_columns(conn, table)
            for column, declaration in columns.items():
                if column not in existing:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")

        now = utc_now()

        # Register every tenant already referenced by legacy data. This makes
        # old knowledge immediately visible to workers and the Admin UI.
        tenant_tables = (
            "raw_objects",
            "knowledge_objects",
            "inbox",
            "entities",
            "relations",
            "feedback",
            "conversations",
            "messages",
            "audit_log",
        )
        tenant_ids: set[str] = set()
        for table in tenant_tables:
            if table not in table_names or "user_id" not in self._table_columns(conn, table):
                continue
            # ``table`` comes from the fixed legacy tenant-table allowlist above.
            rows = conn.execute(
                f"SELECT DISTINCT user_id FROM {table} WHERE user_id IS NOT NULL AND user_id<>''"  # nosec B608
            ).fetchall()
            tenant_ids.update(str(row[0]) for row in rows if row[0])
        for user_id in sorted(tenant_ids):
            conn.execute(
                """INSERT OR IGNORE INTO users(
                       id, source, external_id, display_name, username, preset_key, status,
                       metadata_json, created_at, updated_at, last_seen_at
                   ) VALUES(?, 'legacy', '', '', '', 'user', 'active', ?, ?, ?, ?)""",
                (
                    user_id,
                    json.dumps({"migrated_from_pre_release": True}, sort_keys=True),
                    now,
                    now,
                    now,
                ),
            )

        if "raw_objects" in table_names:
            rows = conn.execute(
                "SELECT id, raw_content FROM raw_objects WHERE content_hash='' OR content_hash IS NULL"
            ).fetchall()
            for row in rows:
                digest = hashlib.sha256(
                    str(row["raw_content"] or "").encode("utf-8", errors="replace")
                ).hexdigest()
                conn.execute(
                    "UPDATE raw_objects SET content_hash=? WHERE id=?",
                    (digest, row["id"]),
                )

        if "knowledge_objects" in table_names:
            # Existing records predate explicit promotion/quality scoring. Keep
            # them searchable, but mark the scores as migrated estimates so the
            # safe cleanup scanner can review them without destructive guesses.
            conn.execute(
                """UPDATE knowledge_objects
                   SET metadata_json=CASE
                         WHEN metadata_json IS NULL OR metadata_json='' THEN '{}'
                         ELSE metadata_json
                       END,
                       knowledge_kind=CASE
                         WHEN knowledge_kind IS NULL OR knowledge_kind='' THEN 'note'
                         ELSE knowledge_kind
                       END,
                       quality_score=CASE
                         WHEN quality_score IS NULL THEN 0.5
                         ELSE MIN(1.0, MAX(0.0, quality_score))
                       END,
                       promotion_score=CASE
                         WHEN promotion_score IS NULL THEN 0.5
                         ELSE MIN(1.0, MAX(0.0, promotion_score))
                       END"""
            )

        if "inbox" in table_names:
            conn.execute(
                """UPDATE inbox
                   SET suggestions_json=CASE
                         WHEN suggestions_json IS NULL OR suggestions_json='' THEN '{}'
                         ELSE suggestions_json
                       END,
                       suggested_action=CASE
                         WHEN suggested_action IS NULL OR suggested_action='' THEN 'review'
                         ELSE suggested_action
                       END,
                       promotion_score=MIN(1.0, MAX(0.0, COALESCE(promotion_score, 0.0))),
                       quality_score=MIN(1.0, MAX(0.0, COALESCE(quality_score, 0.0)))"""
            )

        if "request_idempotency" in table_names:
            conn.execute(
                """UPDATE request_idempotency
                   SET request_hash=COALESCE(request_hash, ''),
                       state=CASE WHEN state='pending' THEN 'pending' ELSE 'complete' END,
                       lease_token=COALESCE(lease_token, ''),
                       response_json=COALESCE(NULLIF(response_json, ''), '{}'),
                       updated_at=CASE
                         WHEN updated_at IS NULL OR updated_at='' THEN created_at
                         ELSE updated_at
                       END"""
            )

        if "conversations" in table_names:
            conn.execute(
                """UPDATE conversations SET mode=CASE
                       WHEN mode IN ('dialogue', 'knowledge_work', 'research') THEN mode
                       ELSE 'dialogue'
                   END"""
            )

        if "channel_sessions" in table_names:
            conn.execute(
                """UPDATE channel_sessions SET mode=CASE
                       WHEN mode IN ('dialogue', 'knowledge_work', 'research') THEN mode
                       ELSE 'dialogue'
                   END"""
            )

        # Schema v8 keeps the append-only feedback history and reconstructs a
        # single current signal per target/type.  Later changes update this
        # projection without deleting the audit trail.
        if {"feedback", "feedback_state"} <= table_names:
            rows = conn.execute(
                """SELECT f.* FROM feedback f
                   JOIN (
                     SELECT user_id, target_type, target_id, feedback_type, MAX(created_at) AS newest
                     FROM feedback
                     GROUP BY user_id, target_type, target_id, feedback_type
                   ) latest
                     ON latest.user_id=f.user_id
                    AND latest.target_type=f.target_type
                    AND latest.target_id=f.target_id
                    AND latest.feedback_type=f.feedback_type
                    AND latest.newest=f.created_at
                   ORDER BY f.id DESC"""
            ).fetchall()
            for row in rows:
                conn.execute(
                    """INSERT OR REPLACE INTO feedback_state(
                           user_id, target_type, target_id, feedback_type, score,
                           comment, context_json, feedback_id, updated_at
                       ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        row["user_id"],
                        row["target_type"],
                        row["target_id"],
                        row["feedback_type"],
                        row["score"],
                        row["comment"],
                        row["context_json"],
                        row["id"],
                        row["created_at"],
                    ),
                )

        if {"knowledge_objects", "knowledge_usage"} <= table_names:
            conn.execute(
                """INSERT OR IGNORE INTO knowledge_usage(
                       user_id, knowledge_object_id, updated_at
                   )
                   SELECT user_id, id, COALESCE(NULLIF(updated_at, ''), created_at)
                   FROM knowledge_objects"""
            )

        if "entities" in table_names:
            # Recompute all values because schema v5 preserves punctuation in compact identifiers.
            # This prevents distinct symbols such as ``ABC.A`` and ``ABC/B`` from collapsing.
            rows = conn.execute("SELECT id, name FROM entities").fetchall()
            for row in rows:
                conn.execute(
                    "UPDATE entities SET normalized_name=? WHERE id=?",
                    (normalize_entity_name(row["name"]), row["id"]),
                )

        if "entity_resolution_candidates" in table_names:
            rows = conn.execute(
                "SELECT id, entity_a_id, entity_b_id FROM entity_resolution_candidates "
                "WHERE pair_key='' OR pair_key IS NULL"
            ).fetchall()
            for row in rows:
                pair_key = "|".join(sorted((row["entity_a_id"], row["entity_b_id"])))
                conn.execute(
                    "UPDATE entity_resolution_candidates SET pair_key=? WHERE id=?",
                    (pair_key, row["id"]),
                )

        # Convert old per-field versions to provenance-rich JSON snapshots.
        if "knowledge_object_versions" in table_names:
            version_columns = self._table_columns(conn, "knowledge_object_versions")
            rows = conn.execute(
                """SELECT v.*, k.user_id AS owner_user_id
                   FROM knowledge_object_versions v
                   JOIN knowledge_objects k ON k.id=v.knowledge_object_id
                   WHERE v.user_id='' OR v.user_id IS NULL
                      OR v.snapshot_json='{}' OR v.snapshot_json IS NULL"""
            ).fetchall()
            for row in rows:
                values = dict(row)
                owner = str(values.pop("owner_user_id") or "")
                snapshot: dict[str, Any] = {
                    key: values.get(key)
                    for key in (
                        "knowledge_object_id",
                        "version",
                        "content",
                        "title",
                        "summary",
                        "tags_json",
                        "importance",
                        "created_at",
                    )
                    if key in version_columns
                }
                snapshot["user_id"] = owner
                conn.execute(
                    "UPDATE knowledge_object_versions SET user_id=?, snapshot_json=? WHERE id=?",
                    (owner, _snapshot(snapshot), row["id"]),
                )

        # Ensure each current object/entity has at least one baseline version.
        if "knowledge_objects" in table_names:
            for row in conn.execute("SELECT * FROM knowledge_objects").fetchall():
                existing = conn.execute(
                    """SELECT 1 FROM knowledge_object_versions
                       WHERE knowledge_object_id=? AND version=? LIMIT 1""",
                    (row["id"], int(row["version"] or 1)),
                ).fetchone()
                if not existing:
                    conn.execute(
                        """INSERT INTO knowledge_object_versions
                           (id, user_id, knowledge_object_id, version, snapshot_json, created_at)
                           VALUES(?, ?, ?, ?, ?, ?)""",
                        (
                            new_id("kov"),
                            row["user_id"],
                            row["id"],
                            int(row["version"] or 1),
                            _snapshot(dict(row)),
                            now,
                        ),
                    )

        if "entities" in table_names:
            for row in conn.execute("SELECT * FROM entities").fetchall():
                conn.execute(
                    """INSERT OR IGNORE INTO entity_versions
                       (id, user_id, entity_id, version, snapshot_json, created_at)
                       VALUES(?, ?, ?, ?, ?, ?)""",
                    (
                        new_id("entv"),
                        row["user_id"],
                        row["id"],
                        int(row["version"] or 1),
                        _snapshot(dict(row)),
                        now,
                    ),
                )

        # Reconstruct links represented by legacy convenience columns.
        if "inbox" in table_names and "knowledge_objects" in table_names:
            conn.execute(
                """UPDATE inbox
                   SET knowledge_object_id=(
                       SELECT k.id FROM knowledge_objects k
                       WHERE k.user_id=inbox.user_id AND k.raw_object_id=inbox.raw_object_id
                       ORDER BY k.version DESC LIMIT 1
                   )
                   WHERE knowledge_object_id IS NULL OR knowledge_object_id=''"""
            )

        if {"knowledge_objects", "entities", "knowledge_entity_links"} <= table_names:
            rows = conn.execute(
                """SELECT k.user_id, k.id AS knowledge_object_id, k.entity_id
                   FROM knowledge_objects k
                   JOIN entities e ON e.id=k.entity_id AND e.user_id=k.user_id
                   WHERE k.entity_id IS NOT NULL AND k.entity_id<>''"""
            ).fetchall()
            for row in rows:
                conn.execute(
                    """INSERT OR IGNORE INTO knowledge_entity_links(
                           id, user_id, knowledge_object_id, entity_id, status, confidence,
                           evidence_json, created_at, reviewed_at, reviewed_by
                       ) VALUES(?, ?, ?, ?, 'accepted', 1.0, ?, ?, ?, 'migration')""",
                    (
                        new_id("kel"),
                        row["user_id"],
                        row["knowledge_object_id"],
                        row["entity_id"],
                        json.dumps({"source": "legacy_entity_id"}, sort_keys=True),
                        now,
                        now,
                    ),
                )

    def close(self) -> None:
        # Shut down every thread's connection (not just the caller's), commit any
        # pending work, then invalidate the per-thread caches by bumping the
        # generation and re-arming the one-time schema init. This makes close()
        # release all WAL locks before a restore swaps the database file, and lets
        # the next use on any thread transparently reopen (and re-migrate) — the
        # reopen-after-close contract restore_backup() depends on.
        #
        # The write lock is taken first (same order as transaction()) so a shutdown
        # drains any in-flight write transaction — e.g. a background worker whose
        # asyncio task was cancelled but whose to_thread() DB call is still running
        # — before its connection is closed, instead of committing a half-written
        # transaction or closing the connection mid-statement.
        with self._write_lock:
            with self._registry_lock:
                connections = list(self._connections)
                self._connections.clear()
                self._generation += 1
                self._schema_ready = False
            for connection in connections:
                with suppress(sqlite3.Error):
                    connection.commit()
                with suppress(sqlite3.Error):
                    connection.close()

    def execute(self, sql: str, params: tuple | dict | None = None) -> sqlite3.Cursor:
        # No lock: this thread's own connection, and the caller fetches the cursor
        # on this same thread, so no cursor is ever stepped across threads.
        return self.conn.execute(sql, params or ())

    def commit(self) -> None:
        self.conn.commit()

    def optimize(self) -> None:
        # PRAGMA optimize runs ANALYZE, which writes sqlite_stat*, so it is a writer
        # and must take the write lock like transaction() — otherwise it contends
        # with a concurrent BEGIN IMMEDIATE at the SQLite level (risking "database is
        # locked") and close() cannot drain it before shutting the connection down.
        with self._write_lock:
            self.conn.execute("PRAGMA optimize")

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        # Serialise writers in Python (single-writer invariant), so two threads'
        # BEGIN IMMEDIATE never contend at the SQLite level and close() can drain
        # the in-flight writer before shutting connections down. Reads never take
        # this lock, so concurrent readers still run in parallel over WAL. The lock
        # is reentrant, so a nested transaction() on the same thread — detected via
        # this thread's own connection's in_transaction — does not self-deadlock.
        with self._write_lock:
            conn = self.conn
            nested = conn.in_transaction
            if not nested:
                conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
                if not nested:
                    conn.commit()
            except Exception:
                if not nested:
                    conn.rollback()
                raise

    # ------------------------------------------------------------------
    # Users and authorization persistence
    # ------------------------------------------------------------------

    def ensure_user(
        self,
        user_id: str,
        *,
        source: str = "local",
        external_id: str = "",
        display_name: str = "",
        username: str = "",
        preset_key: str = "user",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        user_id = validate_user_id(user_id)
        now = utc_now()
        with self.transaction() as conn:
            existing = conn.execute("SELECT metadata_json FROM users WHERE id=?", (user_id,)).fetchone()
            merged_metadata = _json_load(existing["metadata_json"], {}) if existing else {}
            if not isinstance(merged_metadata, dict):
                merged_metadata = {}
            if metadata:
                merged_metadata.update(metadata)
            conn.execute(
                """INSERT INTO users(id, source, external_id, display_name, username, preset_key,
                   status, metadata_json, created_at, updated_at, last_seen_at)
                   VALUES(?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                     source=CASE WHEN excluded.source<>'' THEN excluded.source ELSE users.source END,
                     external_id=CASE WHEN excluded.external_id<>'' THEN excluded.external_id ELSE users.external_id END,
                     display_name=CASE WHEN excluded.display_name<>'' THEN excluded.display_name ELSE users.display_name END,
                     username=CASE WHEN excluded.username<>'' THEN excluded.username ELSE users.username END,
                     metadata_json=excluded.metadata_json,
                     updated_at=excluded.updated_at,
                     last_seen_at=excluded.last_seen_at""",
                (
                    user_id,
                    source,
                    external_id,
                    display_name,
                    username,
                    preset_key,
                    json.dumps(merged_metadata, ensure_ascii=False, sort_keys=True),
                    now,
                    now,
                    now,
                ),
            )
        return self.get_user(user_id) or {}

    def get_user(self, user_id: str) -> dict[str, Any] | None:
        row = self.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        return dict(row) if row else None

    def list_users(self, *, limit: int = 500, offset: int = 0) -> list[dict[str, Any]]:
        rows = self.execute(
            "SELECT * FROM users ORDER BY last_seen_at DESC LIMIT ? OFFSET ?",
            (max(1, min(limit, 5000)), max(0, offset)),
        ).fetchall()
        return [dict(row) for row in rows]

    def list_user_ids(self, *, active_only: bool = True) -> list[str]:
        query = "SELECT id FROM users"
        params: tuple[Any, ...] = ()
        if active_only:
            query += " WHERE status='active'"
        return [row["id"] for row in self.execute(query, params).fetchall()]

    def update_user(self, user_id: str, **fields: Any) -> dict[str, Any] | None:
        allowed = {"display_name", "username", "preset_key", "status", "metadata_json"}
        updates: list[str] = []
        values: list[Any] = []
        for key, value in fields.items():
            if key not in allowed:
                continue
            if key == "metadata_json" and not isinstance(value, str):
                value = json.dumps(value or {}, ensure_ascii=False, sort_keys=True)
            updates.append(f"{key}=?")
            values.append(value)
        if not updates:
            return self.get_user(user_id)
        updates.append("updated_at=?")
        values.extend([utc_now(), user_id])
        with self.transaction() as conn:
            # Column assignments are built only from the fixed ``allowed`` field map above.
            conn.execute(
                f"UPDATE users SET {', '.join(updates)} WHERE id=?",  # nosec B608
                tuple(values),
            )
        return self.get_user(user_id)

    def create_api_token(
        self,
        user_id: str,
        token_sha256: str,
        *,
        label: str = "",
        created_by: str = "",
        ttl_seconds: int | None = None,
    ) -> dict[str, Any]:
        """Store the SHA-256 of a scoped API token; the plaintext is never persisted.

        ``ttl_seconds`` (a positive number of seconds) makes the token expire that
        long after creation; ``None`` mints a non-expiring token. created_at and
        expires_at come from the same instant so the lifetime is exact.
        """
        token_id = new_id("tok")
        now = datetime.now(UTC)
        created_at = now.isoformat(timespec="seconds")
        expires_at: str | None = None
        if ttl_seconds is not None:
            if ttl_seconds <= 0:
                raise ValueError("ttl_seconds must be a positive number of seconds")
            expires_at = (now + timedelta(seconds=ttl_seconds)).isoformat(timespec="seconds")
        with self.transaction() as conn:
            conn.execute(
                """INSERT INTO api_tokens(id, user_id, token_sha256, label, created_by, created_at, expires_at)
                   VALUES(?, ?, ?, ?, ?, ?, ?)""",
                (token_id, user_id, token_sha256, label[:200], created_by, created_at, expires_at),
            )
        row = self.execute("SELECT * FROM api_tokens WHERE id=?", (token_id,)).fetchone()
        return dict(row) if row else {}

    def find_api_token(self, token_sha256: str) -> dict[str, Any] | None:
        """Return the live token row for a SHA-256: non-revoked AND not expired."""
        if not token_sha256:
            return None
        row = self.execute(
            """SELECT * FROM api_tokens
               WHERE token_sha256=? AND revoked_at IS NULL
                 AND (expires_at IS NULL OR expires_at > ?)""",
            (token_sha256, utc_now()),
        ).fetchone()
        return dict(row) if row else None

    def touch_api_token(self, token_id: str) -> None:
        with self.transaction() as conn:
            conn.execute(
                "UPDATE api_tokens SET last_used_at=? WHERE id=?",
                (utc_now(), token_id),
            )

    def list_api_tokens(
        self,
        user_id: str | None = None,
        *,
        include_revoked: bool = False,
    ) -> list[dict[str, Any]]:
        """Token metadata only — never the hash or plaintext."""
        clauses: list[str] = []
        params: list[Any] = []
        if user_id:
            clauses.append("user_id=?")
            params.append(user_id)
        if not include_revoked:
            clauses.append("revoked_at IS NULL")
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.execute(
            "SELECT id, user_id, label, created_by, created_at, last_used_at, revoked_at, expires_at "
            f"FROM api_tokens{where} ORDER BY created_at DESC",  # nosec B608
            tuple(params),
        ).fetchall()
        return [dict(row) for row in rows]

    def revoke_api_token(self, token_id: str, *, user_id: str | None = None) -> bool:
        clause = " AND user_id=?" if user_id else ""
        params: tuple[Any, ...] = (utc_now(), token_id, user_id) if user_id else (utc_now(), token_id)
        with self.transaction() as conn:
            cursor = conn.execute(
                "UPDATE api_tokens SET revoked_at=? WHERE id=? AND revoked_at IS NULL"  # nosec B608
                + clause,
                params,
            )
        return cursor.rowcount > 0

    def upsert_custom_preset(
        self,
        preset_key: str,
        name: str,
        capabilities: set[str],
        *,
        description: str = "",
        created_by: str,
    ) -> dict[str, Any]:
        if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{1,63}", preset_key):
            raise ValueError("Invalid preset key")
        now = utc_now()
        with self.transaction() as conn:
            conn.execute(
                """INSERT INTO custom_presets(preset_key, name, description, created_by, created_at, updated_at)
                   VALUES(?, ?, ?, ?, ?, ?)
                   ON CONFLICT(preset_key) DO UPDATE SET
                     name=excluded.name, description=excluded.description, updated_at=excluded.updated_at""",
                (preset_key, name, description, created_by, now, now),
            )
            conn.execute("DELETE FROM preset_capabilities WHERE preset_key=?", (preset_key,))
            conn.executemany(
                "INSERT INTO preset_capabilities(preset_key, security_id) VALUES(?, ?)",
                [(preset_key, security_id) for security_id in sorted(capabilities)],
            )
        return self.get_custom_preset(preset_key) or {}

    def get_custom_preset(self, preset_key: str) -> dict[str, Any] | None:
        row = self.execute("SELECT * FROM custom_presets WHERE preset_key=?", (preset_key,)).fetchone()
        if not row:
            return None
        result = dict(row)
        result["capabilities"] = [
            item["security_id"]
            for item in self.execute(
                "SELECT security_id FROM preset_capabilities WHERE preset_key=? ORDER BY security_id",
                (preset_key,),
            ).fetchall()
        ]
        return result

    def list_custom_presets(self) -> list[dict[str, Any]]:
        rows = self.execute("SELECT preset_key FROM custom_presets ORDER BY preset_key").fetchall()
        return [item for row in rows if (item := self.get_custom_preset(row["preset_key"]))]

    def set_permission_override(self, user_id: str, security_id: str, effect: str | None) -> None:
        self.ensure_user(user_id)
        with self.transaction() as conn:
            if effect is None:
                conn.execute(
                    "DELETE FROM user_permission_overrides WHERE user_id=? AND security_id=?",
                    (user_id, security_id),
                )
            else:
                if effect not in {"allow", "deny"}:
                    raise ValueError("effect must be allow, deny, or None")
                conn.execute(
                    """INSERT INTO user_permission_overrides(user_id, security_id, effect, updated_at)
                       VALUES(?, ?, ?, ?)
                       ON CONFLICT(user_id, security_id) DO UPDATE SET
                         effect=excluded.effect, updated_at=excluded.updated_at""",
                    (user_id, security_id, effect, utc_now()),
                )

    def get_permission_overrides(self, user_id: str) -> dict[str, str]:
        rows = self.execute(
            "SELECT security_id, effect FROM user_permission_overrides WHERE user_id=?",
            (user_id,),
        ).fetchall()
        return {row["security_id"]: row["effect"] for row in rows}

    # ------------------------------------------------------------------
    # Raw objects and knowledge objects
    # ------------------------------------------------------------------

    def _raw_from_row(self, row: sqlite3.Row | dict[str, Any]) -> RawObject:
        data = dict(row)
        return RawObject(
            id=data["id"],
            user_id=data["user_id"],
            source=data["source"],
            source_ref=data.get("source_ref", ""),
            raw_content=data.get("raw_content", ""),
            content_type=data.get("content_type", "text"),
            metadata_json=_json_load(data.get("metadata_json"), {}),
            content_hash=data.get("content_hash", ""),
            version=int(data.get("version", 1)),
            received_at=data.get("received_at") or utc_now(),
            created_at=data.get("created_at") or utc_now(),
            deleted_at=data.get("deleted_at"),
        )

    def find_raw_by_source_ref(self, user_id: str, source: str, source_ref: str) -> dict[str, Any] | None:
        if not source_ref:
            return None
        row = self.execute(
            "SELECT * FROM raw_objects WHERE user_id=? AND source=? AND source_ref=? AND deleted_at IS NULL",
            (user_id, source, source_ref),
        ).fetchone()
        return dict(row) if row else None

    def store_raw_object(self, obj: RawObject) -> RawObject:
        self.ensure_user(obj.user_id)
        if not obj.content_hash:
            obj.content_hash = hashlib.sha256(obj.raw_content.encode("utf-8", errors="replace")).hexdigest()
        try:
            with self.transaction() as conn:
                conn.execute(
                    """INSERT INTO raw_objects(id, user_id, source, source_ref, raw_content,
                       content_type, metadata_json, content_hash, version, received_at, created_at, deleted_at)
                       VALUES(:id, :user_id, :source, :source_ref, :raw_content,
                       :content_type, :metadata_json, :content_hash, :version, :received_at, :created_at, :deleted_at)""",
                    obj.to_row(),
                )
            return obj
        except sqlite3.IntegrityError:
            existing = self.find_raw_by_source_ref(obj.user_id, obj.source, obj.source_ref)
            if existing:
                existing_hash = str(existing.get("content_hash") or "")
                if (
                    obj.content_hash
                    and existing_hash
                    and not hmac.compare_digest(obj.content_hash, existing_hash)
                ):
                    raise SourceReferenceConflictError(
                        "source_ref is already bound to different content"
                    ) from None
                return self._raw_from_row(existing)
            raise

    def get_raw_object(self, raw_id: str, user_id: str | None = None) -> dict[str, Any] | None:
        if user_id is None:
            row = self.execute("SELECT * FROM raw_objects WHERE id=?", (raw_id,)).fetchone()
        else:
            row = self.execute(
                "SELECT * FROM raw_objects WHERE id=? AND user_id=?", (raw_id, user_id)
            ).fetchone()
        return dict(row) if row else None

    def get_knowledge_by_raw(self, raw_id: str, user_id: str) -> dict[str, Any] | None:
        # The LIVE Knowledge Object for a Raw Object: soft-deleted rows (e.g. a
        # promotion-race loser's orphan, or an ignored KO) are excluded so callers
        # never adopt a hidden object as the canonical one.
        row = self.execute(
            "SELECT * FROM knowledge_objects "
            "WHERE raw_object_id=? AND user_id=? AND deleted_at IS NULL "
            "ORDER BY version DESC LIMIT 1",
            (raw_id, user_id),
        ).fetchone()
        return dict(row) if row else None

    @staticmethod
    def _ko_snapshot(obj: KnowledgeObject | dict[str, Any]) -> dict[str, Any]:
        return obj.to_row() if isinstance(obj, KnowledgeObject) else dict(obj)

    def _store_ko_version(self, conn: sqlite3.Connection, row: dict[str, Any]) -> None:
        conn.execute(
            """INSERT OR IGNORE INTO knowledge_object_versions
               (id, user_id, knowledge_object_id, version, snapshot_json, created_at)
               VALUES(?, ?, ?, ?, ?, ?)""",
            (
                new_id("kov"),
                row["user_id"],
                row["id"],
                int(row.get("version", 1)),
                _snapshot(row),
                utc_now(),
            ),
        )

    def store_knowledge_object(self, obj: KnowledgeObject) -> KnowledgeObject:
        self.ensure_user(obj.user_id)
        raw = self.get_raw_object(obj.raw_object_id, obj.user_id)
        if not raw:
            raise ValueError("KnowledgeObject requires a RawObject owned by the same user")
        row = obj.to_row()
        with self.transaction() as conn:
            conn.execute(
                """INSERT INTO knowledge_objects(id, user_id, raw_object_id, entity_id, content,
                   content_type, title, summary, tags_json, metadata_json, knowledge_kind,
                   importance, quality_score, promotion_score, lifecycle_stage, version,
                   superseded_by_id, created_at, updated_at, deleted_at)
                   VALUES(:id, :user_id, :raw_object_id, :entity_id, :content,
                   :content_type, :title, :summary, :tags_json, :metadata_json, :knowledge_kind,
                   :importance, :quality_score, :promotion_score, :lifecycle_stage, :version,
                   :superseded_by_id, :created_at, :updated_at, :deleted_at)""",
                row,
            )
            self._store_ko_version(conn, row)
        return obj

    def update_knowledge_object(self, obj: KnowledgeObject) -> KnowledgeObject:
        existing = self.get_knowledge_object(obj.id, obj.user_id)
        if not existing:
            raise ValueError("Knowledge object not found for user")
        obj.version = max(int(existing.get("version", 1)) + 1, int(obj.version))
        obj.updated_at = utc_now()
        row = obj.to_row()
        with self.transaction() as conn:
            conn.execute(
                """UPDATE knowledge_objects SET entity_id=:entity_id, content=:content,
                   content_type=:content_type, title=:title, summary=:summary, tags_json=:tags_json,
                   metadata_json=:metadata_json, knowledge_kind=:knowledge_kind,
                   importance=:importance, quality_score=:quality_score,
                   promotion_score=:promotion_score, lifecycle_stage=:lifecycle_stage, version=:version,
                   superseded_by_id=:superseded_by_id, updated_at=:updated_at, deleted_at=:deleted_at
                   WHERE id=:id AND user_id=:user_id""",
                row,
            )
            self._store_ko_version(conn, row)
        return obj

    def update_knowledge_fields(self, ko_id: str, user_id: str, **fields: Any) -> dict[str, Any] | None:
        current = self.get_knowledge_object(ko_id, user_id)
        if not current:
            return None
        tags = fields.get("tags_json", _json_load(current.get("tags_json"), []))
        metadata = fields.get("metadata_json", _json_load(current.get("metadata_json"), {}))
        obj = KnowledgeObject(
            id=current["id"],
            user_id=current["user_id"],
            raw_object_id=current["raw_object_id"],
            entity_id=fields.get("entity_id", current.get("entity_id")),
            content=fields.get("content", current.get("content", "")),
            content_type=fields.get("content_type", current.get("content_type", "")),
            title=fields.get("title", current.get("title", "")),
            summary=fields.get("summary", current.get("summary", "")),
            tags_json=tags if isinstance(tags, list) else _json_load(tags, []),
            metadata_json=metadata if isinstance(metadata, dict) else _json_load(metadata, {}),
            knowledge_kind=str(fields.get("knowledge_kind", current.get("knowledge_kind", "note"))),
            importance=float(fields.get("importance", current.get("importance", 0.5))),
            quality_score=float(fields.get("quality_score", current.get("quality_score", 0.5))),
            promotion_score=float(fields.get("promotion_score", current.get("promotion_score", 0.5))),
            lifecycle_stage=fields.get("lifecycle_stage", current.get("lifecycle_stage", "active")),
            version=int(current.get("version", 1)),
            superseded_by_id=fields.get("superseded_by_id", current.get("superseded_by_id")),
            created_at=current.get("created_at", utc_now()),
            updated_at=current.get("updated_at", utc_now()),
            deleted_at=fields.get("deleted_at", current.get("deleted_at")),
        )
        self.update_knowledge_object(obj)
        return self.get_knowledge_object(ko_id, user_id)

    def get_knowledge_object(self, ko_id: str, user_id: str | None = None) -> dict[str, Any] | None:
        if user_id is None:
            row = self.execute("SELECT * FROM knowledge_objects WHERE id=?", (ko_id,)).fetchone()
        else:
            row = self.execute(
                "SELECT * FROM knowledge_objects WHERE id=? AND user_id=?",
                (ko_id, user_id),
            ).fetchone()
        return dict(row) if row else None

    def list_knowledge_versions(self, ko_id: str, user_id: str) -> list[dict[str, Any]]:
        rows = self.execute(
            """SELECT * FROM knowledge_object_versions
               WHERE knowledge_object_id=? AND user_id=? ORDER BY version DESC""",
            (ko_id, user_id),
        ).fetchall()
        return [dict(row) for row in rows]

    def diff_knowledge_versions(
        self,
        ko_id: str,
        user_id: str,
        *,
        from_version: int | None = None,
        to_version: int | None = None,
    ) -> dict[str, Any] | None:
        """Structured diff between two versions (default: the two most recent)."""
        from jericho.versions import diff_snapshots

        versions = self.list_knowledge_versions(ko_id, user_id)  # newest first
        if not versions:
            return None
        by_version = {int(row["version"]): _json_load(row.get("snapshot_json"), {}) for row in versions}
        available = sorted(by_version)
        newest = available[-1]
        target = to_version if to_version is not None else newest
        if target not in by_version:
            return None
        base = from_version
        if base is None:
            earlier = [v for v in available if v < target]
            base = earlier[-1] if earlier else target
        if base not in by_version:
            return None
        return {
            "knowledge_object_id": ko_id,
            "from_version": base,
            "to_version": target,
            "available_versions": available,
            "changes": diff_snapshots(by_version[base], by_version[target]),
        }

    def count_knowledge_objects(self, user_id: str) -> int:
        row = self.execute(
            "SELECT COUNT(*) AS count FROM knowledge_objects WHERE user_id=? AND deleted_at IS NULL",
            (user_id,),
        ).fetchone()
        return int(row["count"] if row else 0)

    def list_knowledge_objects(
        self,
        user_id: str,
        *,
        limit: int = 100,
        offset: int = 0,
        lifecycle_stage: str | None = None,
        tag: str | None = None,
        entity_id: str | None = None,
    ) -> list[dict[str, Any]]:
        params: list[Any] = [user_id]
        where = "user_id=? AND deleted_at IS NULL"
        if lifecycle_stage:
            where += " AND lifecycle_stage=?"
            params.append(lifecycle_stage)
        if tag:
            # ``tags_json`` is a canonical JSON array, so json_each enumerates
            # exact tags; jericho_casefold keeps the match case-insensitive for
            # Cyrillic as well (SQLite's lower() folds ASCII only).
            where += (
                " AND EXISTS (SELECT 1 FROM json_each(knowledge_objects.tags_json)"
                " WHERE jericho_casefold(json_each.value) = jericho_casefold(?))"
            )
            params.append(tag)
        if entity_id:
            # Browse-by-entity/container: only reviewer-accepted links count.
            where += (
                " AND EXISTS (SELECT 1 FROM knowledge_entity_links l"
                " WHERE l.knowledge_object_id = knowledge_objects.id"
                " AND l.entity_id=? AND l.user_id=? AND l.status='accepted')"
            )
            params.extend([entity_id, user_id])
        params.extend([max(1, min(limit, 5000)), max(0, offset)])
        # ``where`` contains only fixed clauses; all values remain bound parameters.
        rows = self.execute(
            f"SELECT * FROM knowledge_objects WHERE {where} ORDER BY importance DESC, updated_at DESC LIMIT ? OFFSET ?",  # nosec B608
            tuple(params),
        ).fetchall()
        return [dict(row) for row in rows]

    def list_knowledge_tags(self, user_id: str, *, limit: int = 200) -> list[dict[str, Any]]:
        """Distinct tags with usage counts for browse-by-tag surfaces.

        Tags are stored canonically (deduped, casefold-sorted) per object, so
        one json_each pass yields exact values; grouping is case-insensitive
        with the first-seen spelling kept for display.
        """
        rows = self.execute(
            "SELECT json_each.value AS tag, COUNT(*) AS count"
            " FROM knowledge_objects, json_each(knowledge_objects.tags_json)"
            " WHERE knowledge_objects.user_id=? AND knowledge_objects.deleted_at IS NULL"
            " GROUP BY jericho_casefold(json_each.value)"
            " ORDER BY count DESC, jericho_casefold(json_each.value) ASC LIMIT ?",
            (user_id, max(1, min(int(limit), 1000))),
        ).fetchall()
        return [{"tag": str(row["tag"]), "count": int(row["count"])} for row in rows]

    def list_container_entities(self, user_id: str, types: tuple[str, ...]) -> list[dict[str, Any]]:
        """Canonical container entities (projects/collections) with member counts."""
        if not types:
            return []
        placeholders = ",".join("?" for _ in types)
        rows = self.execute(
            "SELECT e.*, ("
            " SELECT COUNT(*) FROM knowledge_entity_links l"
            " JOIN knowledge_objects k ON k.id = l.knowledge_object_id"
            " WHERE l.entity_id = e.id AND l.user_id = e.user_id"
            " AND l.status='accepted' AND k.deleted_at IS NULL"
            ") AS knowledge_count"
            f" FROM entities e WHERE e.user_id=? AND e.entity_type IN ({placeholders})"  # nosec B608
            " AND e.deleted_at IS NULL AND e.canonical=1"
            " ORDER BY lower(e.name) ASC",
            (user_id, *types),
        ).fetchall()
        return [dict(row) for row in rows]

    def list_part_of_relations(self, user_id: str) -> list[dict[str, Any]]:
        """Active PART_OF edges; source is the child, target the parent."""
        rows = self.execute(
            "SELECT source_entity_id, target_entity_id, weight FROM relations"
            " WHERE user_id=? AND relation_type=? AND deleted_at IS NULL"
            " ORDER BY weight DESC",
            (user_id, RelationType.PART_OF.value),
        ).fetchall()
        return [dict(row) for row in rows]

    def list_entities_by_activity(
        self,
        user_id: str,
        *,
        types: tuple[str, ...] | None = None,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        """Canonical entities ranked by how many live knowledge objects link them.

        The signal behind "recurring people/projects" in a user model: an entity
        the user keeps attaching material to. Only accepted links and non-deleted
        knowledge count.
        """
        clauses = ["e.user_id=?", "e.deleted_at IS NULL", "e.canonical=1"]
        params: list[Any] = [user_id]
        if types:
            placeholders = ",".join("?" for _ in types)
            clauses.append(f"e.entity_type IN ({placeholders})")  # nosec B608
            params.extend(types)
        params.append(max(1, min(int(limit), 100)))
        rows = self.execute(
            "SELECT e.id AS id, e.name AS name, e.entity_type AS entity_type,"
            " COUNT(l.id) AS knowledge_count"
            " FROM entities e"
            " JOIN knowledge_entity_links l"
            "   ON l.entity_id = e.id AND l.user_id = e.user_id AND l.status='accepted'"
            " JOIN knowledge_objects k"
            "   ON k.id = l.knowledge_object_id AND k.deleted_at IS NULL"
            f" WHERE {' AND '.join(clauses)}"  # nosec B608
            " GROUP BY e.id ORDER BY knowledge_count DESC, lower(e.name) ASC LIMIT ?",
            tuple(params),
        ).fetchall()
        return [dict(row) for row in rows]

    def list_recent_knowledge(self, user_id: str, *, since_iso: str, limit: int = 10) -> list[dict[str, Any]]:
        """Knowledge created at or after ``since_iso`` — the "what happened lately" window."""
        rows = self.execute(
            "SELECT id, title, knowledge_kind, created_at FROM knowledge_objects"
            " WHERE user_id=? AND deleted_at IS NULL AND created_at >= ?"
            " ORDER BY created_at DESC LIMIT ?",
            (user_id, since_iso, max(1, min(int(limit), 200))),
        ).fetchall()
        return [dict(row) for row in rows]

    def list_knowledge_on_this_day(
        self, user_id: str, *, month_day: str, before_iso: str, limit: int = 10
    ) -> list[dict[str, Any]]:
        """Knowledge captured on the same calendar day (MM-DD) in an earlier year.

        ``created_at`` is an ISO string, so ``strftime('%m-%d', …)`` selects the
        anniversary and the ``created_at < before_iso`` bound keeps only the past.
        """
        rows = self.execute(
            "SELECT id, title, knowledge_kind, created_at FROM knowledge_objects"
            " WHERE user_id=? AND deleted_at IS NULL"
            " AND strftime('%m-%d', created_at) = ? AND created_at < ?"
            " ORDER BY created_at DESC LIMIT ?",
            (user_id, month_day, before_iso, max(1, min(int(limit), 200))),
        ).fetchall()
        return [dict(row) for row in rows]

    def soft_delete_knowledge_object(self, ko_id: str, user_id: str | None = None) -> bool:
        """Soft-delete an object while retaining a complete version snapshot."""
        current = self.get_knowledge_object(ko_id, user_id)
        if not current or current.get("deleted_at"):
            return False
        owner = str(current["user_id"])
        updated = self.update_knowledge_fields(
            ko_id,
            owner,
            lifecycle_stage=LifecycleStage.DELETED.value,
            deleted_at=utc_now(),
        )
        return updated is not None

    def list_purgeable_knowledge(
        self,
        user_id: str | None = None,
        *,
        older_than_days: int = 30,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        """Soft-deleted objects whose retention window has elapsed and may be purged."""
        cutoff = f"-{max(0, int(older_than_days))} days"
        bounded = max(1, min(int(limit), 2000))
        base = (
            "SELECT id, user_id, title, deleted_at FROM knowledge_objects "
            "WHERE deleted_at IS NOT NULL AND datetime(deleted_at) <= datetime('now', ?)"
        )
        if user_id:
            rows = self.execute(
                f"{base} AND user_id=? ORDER BY deleted_at LIMIT ?",
                (cutoff, user_id, bounded),
            ).fetchall()
        else:
            rows = self.execute(
                f"{base} ORDER BY deleted_at LIMIT ?",
                (cutoff, bounded),
            ).fetchall()
        return [dict(row) for row in rows]

    def purge_knowledge_object(
        self,
        ko_id: str,
        user_id: str | None = None,
        *,
        require_soft_deleted: bool = True,
    ) -> dict[str, Any]:
        """Irreversibly hard-delete one Knowledge Object and every trace of it.

        Child rows are removed in foreign-key dependency order (versions, entity
        links, usage, embeddings, inbox, conflicts, feedback); dangling supersession
        pointers are nulled; then the object itself is deleted, and its ``AFTER
        DELETE`` trigger removes the FTS entry automatically. The backing Raw Object
        (and its on-disk file, reported to the caller) is removed only when no other
        object or inbox row still references it, and never when the content-addressed
        file is shared by another Raw Object. This is the one place the system
        intentionally destroys provenance, so it refuses anything not already
        soft-deleted unless explicitly overridden.
        """
        current = self.get_knowledge_object(ko_id, user_id)
        if not current:
            return {"existed": False, "knowledge_object_id": ko_id}
        owner = str(current["user_id"])
        if require_soft_deleted and not current.get("deleted_at"):
            raise ValueError("Knowledge object must be soft-deleted before it can be purged")
        raw_object_id = str(current.get("raw_object_id") or "")
        deleted: dict[str, int] = {}
        raw_removed = False
        raw_file_path = ""
        unlink_file = False
        with self.transaction() as conn:

            def _del(table: str, where: str, params: tuple[Any, ...]) -> None:
                cursor = conn.execute(f"DELETE FROM {table} WHERE {where}", params)  # nosec B608
                if cursor.rowcount:
                    deleted[table] = deleted.get(table, 0) + cursor.rowcount

            _del("knowledge_embeddings", "knowledge_object_id=? AND user_id=?", (ko_id, owner))
            _del("knowledge_usage", "knowledge_object_id=? AND user_id=?", (ko_id, owner))
            _del("knowledge_entity_links", "knowledge_object_id=? AND user_id=?", (ko_id, owner))
            _del(
                "knowledge_conflicts",
                "user_id=? AND (knowledge_a_id=? OR knowledge_b_id=?)",
                (owner, ko_id, ko_id),
            )
            _del("knowledge_object_versions", "knowledge_object_id=? AND user_id=?", (ko_id, owner))
            _del("inbox", "knowledge_object_id=? AND user_id=?", (ko_id, owner))
            # feedback_state references feedback(id), so it must go first.
            _del("feedback_state", "target_id=? AND user_id=?", (ko_id, owner))
            _del("feedback", "target_id=? AND user_id=?", (ko_id, owner))
            conn.execute(
                "UPDATE knowledge_objects SET superseded_by_id=NULL WHERE user_id=? AND superseded_by_id=?",
                (owner, ko_id),
            )
            cursor = conn.execute("DELETE FROM knowledge_objects WHERE id=? AND user_id=?", (ko_id, owner))
            deleted["knowledge_objects"] = cursor.rowcount

            if raw_object_id:
                still_used = (
                    conn.execute(
                        "SELECT 1 FROM knowledge_objects WHERE raw_object_id=? LIMIT 1",
                        (raw_object_id,),
                    ).fetchone()
                    or conn.execute(
                        "SELECT 1 FROM inbox WHERE raw_object_id=? LIMIT 1",
                        (raw_object_id,),
                    ).fetchone()
                )
                if not still_used:
                    raw_row = conn.execute(
                        "SELECT metadata_json, content_type, content_hash FROM raw_objects "
                        "WHERE id=? AND user_id=?",
                        (raw_object_id, owner),
                    ).fetchone()
                    if raw_row is not None:
                        metadata = _json_load(raw_row["metadata_json"], {})
                        raw_file_path = str(metadata.get("stored_path") or "")
                        if str(raw_row["content_type"]) == "file" and raw_file_path:
                            shared = conn.execute(
                                "SELECT 1 FROM raw_objects WHERE user_id=? AND content_type='file' "
                                "AND content_hash=? AND id<>? LIMIT 1",
                                (owner, str(raw_row["content_hash"] or ""), raw_object_id),
                            ).fetchone()
                            unlink_file = shared is None
                        conn.execute(
                            "DELETE FROM raw_objects WHERE id=? AND user_id=?",
                            (raw_object_id, owner),
                        )
                        deleted["raw_objects"] = 1
                        raw_removed = True
        return {
            "existed": True,
            "knowledge_object_id": ko_id,
            "user_id": owner,
            "title": str(current.get("title") or ""),
            "raw_object_id": raw_object_id,
            "raw_removed": raw_removed,
            "raw_file_path": raw_file_path,
            "unlink_file": unlink_file,
            "deleted": deleted,
        }

    def search_knowledge(self, user_id: str, query: str, *, limit: int = 20) -> list[dict[str, Any]]:
        text = " ".join((query or "").split()).strip()
        if not text:
            return []
        rows: list[sqlite3.Row] = []
        terms = re.findall(r"[\w#+.-]{2,}", text, flags=re.UNICODE)[:12]
        if self._fts_available and terms:
            match_query = " OR ".join(f'"{term.replace(chr(34), chr(34) * 2)}"*' for term in terms)
            try:
                rows = self.execute(
                    """SELECT k.*, bm25(knowledge_fts, 1.0, 2.0, 1.5, 0.5) AS _rank
                       FROM knowledge_fts
                       JOIN knowledge_objects k ON k.rowid=knowledge_fts.rowid
                       WHERE k.user_id=? AND k.deleted_at IS NULL AND knowledge_fts MATCH ?
                       ORDER BY _rank ASC, k.importance DESC LIMIT ?""",
                    (user_id, match_query, max(1, min(limit, 200))),
                ).fetchall()
            except sqlite3.OperationalError:
                rows = []
        if not rows:
            escaped = text.replace("%", r"\%").replace("_", r"\_")
            like = f"%{escaped}%"
            rows = self.execute(
                """SELECT * FROM knowledge_objects
                   WHERE user_id=? AND deleted_at IS NULL
                     AND (title LIKE ? ESCAPE '\\' OR summary LIKE ? ESCAPE '\\'
                          OR content LIKE ? ESCAPE '\\' OR tags_json LIKE ? ESCAPE '\\')
                   ORDER BY importance DESC, updated_at DESC LIMIT ?""",
                (user_id, like, like, like, like, max(1, min(limit, 200))),
            ).fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------
    # Inbox
    # ------------------------------------------------------------------

    def store_inbox_item(self, item: InboxItem) -> InboxItem:
        self.ensure_user(item.user_id)
        raw = self.get_raw_object(item.raw_object_id, item.user_id)
        if not raw:
            raise ValueError("Inbox item requires a RawObject owned by the same user")
        with self.transaction() as conn:
            conn.execute(
                """INSERT INTO inbox(id, user_id, raw_object_id, knowledge_object_id, status,
                   suggested_entity_id, suggested_tags_json, suggestions_json, suggested_action,
                   promotion_score, quality_score, classification_notes, created_at,
                   reviewed_at, reviewed_by)
                   VALUES(:id, :user_id, :raw_object_id, :knowledge_object_id, :status,
                   :suggested_entity_id, :suggested_tags_json, :suggestions_json, :suggested_action,
                   :promotion_score, :quality_score, :classification_notes, :created_at,
                   :reviewed_at, :reviewed_by)""",
                item.to_row(),
            )
        return item

    def get_inbox_item(self, inbox_id: str, user_id: str) -> dict[str, Any] | None:
        row = self.execute("SELECT * FROM inbox WHERE id=? AND user_id=?", (inbox_id, user_id)).fetchone()
        return dict(row) if row else None

    def get_inbox_by_raw(self, raw_object_id: str, user_id: str) -> dict[str, Any] | None:
        row = self.execute(
            "SELECT * FROM inbox WHERE raw_object_id=? AND user_id=? ORDER BY created_at DESC LIMIT 1",
            (raw_object_id, user_id),
        ).fetchone()
        return dict(row) if row else None

    def find_inbox_by_raw(self, raw_object_id: str, user_id: str) -> dict[str, Any] | None:
        row = self.execute(
            """SELECT * FROM inbox WHERE raw_object_id=? AND user_id=?
               ORDER BY created_at DESC LIMIT 1""",
            (raw_object_id, user_id),
        ).fetchone()
        return dict(row) if row else None

    def list_inbox(
        self,
        user_id: str,
        status: InboxStatus | None = None,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        if status:
            rows = self.execute(
                """SELECT * FROM inbox WHERE user_id=? AND status=?
                   ORDER BY created_at DESC LIMIT ? OFFSET ?""",
                (user_id, enum_value(status), max(1, min(limit, 1000)), max(0, offset)),
            ).fetchall()
        else:
            rows = self.execute(
                "SELECT * FROM inbox WHERE user_id=? ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (user_id, max(1, min(limit, 1000)), max(0, offset)),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_inbox_detailed(
        self,
        user_id: str,
        status: InboxStatus | None = None,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        clauses = ["i.user_id=?"]
        params: list[Any] = [user_id]
        if status:
            clauses.append("i.status=?")
            params.append(enum_value(status))
        params.extend([max(1, min(limit, 1000)), max(0, offset)])
        # ``clauses`` contains only fixed predicates; all values remain bound parameters.
        query = f"""SELECT i.*, r.source, r.source_ref, r.raw_content, r.content_type AS raw_content_type,
                       r.metadata_json AS raw_metadata_json, r.received_at,
                       k.title AS knowledge_title, k.summary AS knowledge_summary,
                       k.knowledge_kind, k.importance, k.quality_score AS knowledge_quality_score,
                       k.promotion_score AS knowledge_promotion_score, k.lifecycle_stage
                FROM inbox i
                JOIN raw_objects r ON r.id=i.raw_object_id AND r.user_id=i.user_id
                LEFT JOIN knowledge_objects k
                  ON k.id=i.knowledge_object_id AND k.user_id=i.user_id
                WHERE {" AND ".join(clauses)}
                ORDER BY CASE i.status WHEN 'pending' THEN 0 ELSE 1 END,
                         i.promotion_score DESC, i.created_at DESC
                LIMIT ? OFFSET ?"""  # nosec B608
        rows = self.execute(query, tuple(params)).fetchall()
        return [dict(row) for row in rows]

    def update_inbox_status(
        self,
        inbox_id: str,
        status: InboxStatus,
        reviewed_by: str | None = None,
        *,
        user_id: str | None = None,
        suggested_entity_id: str | None = None,
        suggested_tags: list[str] | None = None,
        suggestions: dict[str, Any] | None = None,
        suggested_action: str | None = None,
        knowledge_object_id: str | None = None,
        clear_knowledge_object_id: bool = False,
        promotion_score: float | None = None,
        quality_score: float | None = None,
        notes: str | None = None,
    ) -> bool:
        updates = ["status=?", "reviewed_at=?", "reviewed_by=?"]
        values: list[Any] = [enum_value(status), utc_now(), reviewed_by]
        if suggested_entity_id is not None:
            updates.append("suggested_entity_id=?")
            values.append(suggested_entity_id)
        if suggested_tags is not None:
            updates.append("suggested_tags_json=?")
            values.append(json.dumps(sorted(set(suggested_tags)), ensure_ascii=False))
        if suggestions is not None:
            updates.append("suggestions_json=?")
            values.append(json.dumps(suggestions, ensure_ascii=False, sort_keys=True))
        if suggested_action is not None:
            updates.append("suggested_action=?")
            values.append(str(suggested_action)[:32])
        if clear_knowledge_object_id:
            updates.append("knowledge_object_id=NULL")
        elif knowledge_object_id is not None:
            updates.append("knowledge_object_id=?")
            values.append(knowledge_object_id)
        if promotion_score is not None:
            updates.append("promotion_score=?")
            values.append(max(0.0, min(1.0, float(promotion_score))))
        if quality_score is not None:
            updates.append("quality_score=?")
            values.append(max(0.0, min(1.0, float(quality_score))))
        if notes is not None:
            updates.append("classification_notes=?")
            values.append(notes)
        # Assignment fragments are selected from fixed fields in this method.
        query = f"UPDATE inbox SET {', '.join(updates)} WHERE id=?"  # nosec B608
        values.append(inbox_id)
        if user_id is not None:
            query += " AND user_id=?"
            values.append(user_id)
        with self.transaction() as conn:
            cursor = conn.execute(query, tuple(values))
        return cursor.rowcount > 0

    def claim_inbox_promotion(self, inbox_id: str, user_id: str, knowledge_object_id: str) -> bool:
        """Atomically reserve an Inbox item for promotion.

        Sets ``knowledge_object_id`` only if the item still has none, so exactly
        one of several concurrent approvals wins; the losers get ``False`` and
        must NOT create a second canonical Knowledge Object from one Raw Object
        (the "Inbox before canonical, exactly once" invariant).
        """
        with self.transaction() as conn:
            cursor = conn.execute(
                "UPDATE inbox SET knowledge_object_id=? "
                "WHERE id=? AND user_id=? AND knowledge_object_id IS NULL",
                (knowledge_object_id, inbox_id, user_id),
            )
        return cursor.rowcount == 1

    def update_inbox_suggestions(
        self,
        inbox_id: str,
        user_id: str,
        *,
        suggestions: dict[str, Any],
        suggested_tags: list[str] | None = None,
        suggested_action: str | None = None,
        promotion_score: float | None = None,
        quality_score: float | None = None,
        classification_notes: str | None = None,
    ) -> bool:
        """Refresh machine-generated Inbox advice without marking it human-reviewed.

        Background enrichment must not change ``status``, ``reviewed_at``, or
        ``reviewed_by``.  Keeping this operation separate from
        :meth:`update_inbox_status` makes that policy explicit and prevents a
        model-generated suggestion from looking like an administrator decision.
        """

        updates = ["suggestions_json=?"]
        values: list[Any] = [json.dumps(suggestions, ensure_ascii=False, sort_keys=True)]
        if suggested_tags is not None:
            updates.append("suggested_tags_json=?")
            values.append(json.dumps(sorted(set(suggested_tags)), ensure_ascii=False))
        if suggested_action is not None:
            updates.append("suggested_action=?")
            values.append(str(suggested_action).strip().casefold()[:32] or "review")
        if promotion_score is not None:
            updates.append("promotion_score=?")
            values.append(max(0.0, min(1.0, float(promotion_score))))
        if quality_score is not None:
            updates.append("quality_score=?")
            values.append(max(0.0, min(1.0, float(quality_score))))
        if classification_notes is not None:
            updates.append("classification_notes=?")
            values.append(str(classification_notes)[:4000])
        values.extend([inbox_id, user_id])
        with self.transaction() as conn:
            # Assignment fragments are selected from fixed fields in this method.
            cursor = conn.execute(
                f"UPDATE inbox SET {', '.join(updates)} WHERE id=? AND user_id=?",  # nosec B608
                tuple(values),
            )
        return cursor.rowcount > 0

    # ------------------------------------------------------------------
    # Knowledge graph
    # ------------------------------------------------------------------

    def _store_entity_version(self, conn: sqlite3.Connection, row: dict[str, Any]) -> None:
        conn.execute(
            """INSERT OR IGNORE INTO entity_versions
               (id, user_id, entity_id, version, snapshot_json, created_at)
               VALUES(?, ?, ?, ?, ?, ?)""",
            (
                new_id("entv"),
                row["user_id"],
                row["id"],
                int(row.get("version", 1)),
                _snapshot(row),
                utc_now(),
            ),
        )

    def create_entity(self, entity: Entity) -> Entity:
        self.ensure_user(entity.user_id)
        row = entity.to_row()
        row["normalized_name"] = normalize_entity_name(entity.name)
        if not row["normalized_name"]:
            raise ValueError("Entity name is empty after normalization")
        with self.transaction() as conn:
            conn.execute(
                """INSERT INTO entities(id, user_id, name, normalized_name, entity_type,
                   aliases_json, description, metadata_json, canonical, merged_into_id, version,
                   created_at, updated_at, deleted_at)
                   VALUES(:id, :user_id, :name, :normalized_name, :entity_type,
                   :aliases_json, :description, :metadata_json, :canonical, :merged_into_id, :version,
                   :created_at, :updated_at, :deleted_at)""",
                row,
            )
            self._store_entity_version(conn, row)
        return entity

    def update_entity(self, entity: Entity) -> Entity:
        existing = self.get_entity(entity.id, entity.user_id)
        if not existing:
            raise ValueError("Entity not found for user")
        entity.version = max(int(existing.get("version", 1)) + 1, int(entity.version))
        entity.updated_at = utc_now()
        row = entity.to_row()
        row["normalized_name"] = normalize_entity_name(entity.name)
        with self.transaction() as conn:
            conn.execute(
                """UPDATE entities SET name=:name, normalized_name=:normalized_name,
                   entity_type=:entity_type, aliases_json=:aliases_json, description=:description,
                   metadata_json=:metadata_json, canonical=:canonical, merged_into_id=:merged_into_id,
                   version=:version, updated_at=:updated_at, deleted_at=:deleted_at
                   WHERE id=:id AND user_id=:user_id""",
                row,
            )
            self._store_entity_version(conn, row)
        return entity

    def get_entity(self, entity_id: str, user_id: str | None = None) -> dict[str, Any] | None:
        if user_id is None:
            row = self.execute("SELECT * FROM entities WHERE id=?", (entity_id,)).fetchone()
        else:
            row = self.execute(
                "SELECT * FROM entities WHERE id=? AND user_id=?", (entity_id, user_id)
            ).fetchone()
        return dict(row) if row else None

    def list_entity_versions(self, entity_id: str, user_id: str) -> list[dict[str, Any]]:
        rows = self.execute(
            "SELECT * FROM entity_versions WHERE entity_id=? AND user_id=? ORDER BY version DESC",
            (entity_id, user_id),
        ).fetchall()
        return [dict(row) for row in rows]

    def list_entities(
        self,
        user_id: str,
        entity_type: EntityType | None = None,
        *,
        limit: int = 100,
        include_merged: bool = False,
    ) -> list[dict[str, Any]]:
        where = "user_id=?"
        params: list[Any] = [user_id]
        if not include_merged:
            where += " AND deleted_at IS NULL AND canonical=1"
        if entity_type:
            where += " AND entity_type=?"
            params.append(enum_value(entity_type))
        params.append(max(1, min(limit, 5000)))
        # ``where`` contains only fixed predicates; values remain bound.
        rows = self.execute(
            f"SELECT * FROM entities WHERE {where} ORDER BY name COLLATE NOCASE LIMIT ?",  # nosec B608
            tuple(params),
        ).fetchall()
        return [dict(row) for row in rows]

    def find_entity_by_name(self, user_id: str, name: str) -> dict[str, Any] | None:
        normalized = normalize_entity_name(name)
        row = self.execute(
            """SELECT * FROM entities WHERE user_id=? AND normalized_name=?
               AND deleted_at IS NULL AND canonical=1 ORDER BY updated_at DESC LIMIT 1""",
            (user_id, normalized),
        ).fetchone()
        return dict(row) if row else None

    def find_entity_by_alias(self, user_id: str, alias: str) -> list[dict[str, Any]]:
        normalized = normalize_entity_name(alias)
        results: list[dict[str, Any]] = []
        for row in self.list_entities(user_id, limit=5000):
            aliases = _json_load(row.get("aliases_json"), [])
            if any(normalize_entity_name(item) == normalized for item in aliases):
                results.append(row)
        return results

    def soft_delete_entity(self, entity_id: str, user_id: str | None = None) -> bool:
        """Soft-delete an entity and record the state as a new entity version."""
        current = self.get_entity(entity_id, user_id)
        if not current or current.get("deleted_at"):
            return False
        entity = Entity(
            id=str(current["id"]),
            user_id=str(current["user_id"]),
            name=str(current.get("name") or ""),
            entity_type=str(current.get("entity_type") or EntityType.OTHER.value),
            aliases_json=_json_load(current.get("aliases_json"), []),
            description=str(current.get("description") or ""),
            metadata_json=_json_load(current.get("metadata_json"), {}),
            canonical=False,
            merged_into_id=current.get("merged_into_id"),
            version=int(current.get("version", 1)),
            created_at=str(current.get("created_at") or utc_now()),
            updated_at=str(current.get("updated_at") or utc_now()),
            deleted_at=utc_now(),
        )
        self.update_entity(entity)
        return True

    def set_entity_time(
        self,
        entity_id: str,
        user_id: str,
        occurred_at: str,
        *,
        occurred_end: str | None = None,
        precision: str = "day",
        source: str = "",
    ) -> dict[str, Any]:
        """Record (or replace) the temporal anchor of an event entity."""
        with self.transaction() as conn:
            conn.execute(
                """INSERT INTO entity_time(entity_id, user_id, occurred_at, occurred_end,
                   precision, source, updated_at)
                   VALUES(?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(entity_id) DO UPDATE SET
                     occurred_at=excluded.occurred_at,
                     occurred_end=excluded.occurred_end,
                     precision=excluded.precision,
                     source=excluded.source,
                     updated_at=excluded.updated_at""",
                (entity_id, user_id, occurred_at, occurred_end, precision, source, utc_now()),
            )
        return self.get_entity_time(entity_id, user_id) or {}

    def get_entity_time(self, entity_id: str, user_id: str) -> dict[str, Any] | None:
        row = self.execute(
            "SELECT * FROM entity_time WHERE entity_id=? AND user_id=?",
            (entity_id, user_id),
        ).fetchone()
        return dict(row) if row else None

    def delete_entity_time(self, entity_id: str, user_id: str | None = None) -> bool:
        clause = " AND user_id=?" if user_id else ""
        params: tuple[Any, ...] = (entity_id, user_id) if user_id else (entity_id,)
        with self.transaction() as conn:
            cursor = conn.execute(
                f"DELETE FROM entity_time WHERE entity_id=?{clause}",  # nosec B608
                params,
            )
        return cursor.rowcount > 0

    def list_events_in_range(
        self,
        user_id: str,
        *,
        start: str | None = None,
        end: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        """Ordered timeline of dated event entities, optionally bounded to a window.

        Only canonical, non-deleted event entities are returned; a merged or deleted
        event cannot resurface on the timeline through a stale temporal row.
        """
        clauses = [
            "e.user_id=?",
            "e.entity_type='event'",
            "e.canonical=1",
            "e.deleted_at IS NULL",
        ]
        params: list[Any] = [user_id]
        if start:
            clauses.append("t.occurred_at >= ?")
            params.append(start)
        if end:
            clauses.append("t.occurred_at <= ?")
            params.append(end)
        params.append(max(1, min(int(limit), 2000)))
        rows = self.execute(
            "SELECT e.id AS entity_id, e.name AS name, e.entity_type AS entity_type, "
            "e.description AS description, t.occurred_at AS occurred_at, "
            "t.occurred_end AS occurred_end, t.precision AS precision, t.source AS source "
            "FROM entity_time t JOIN entities e ON e.id=t.entity_id "
            f"WHERE {' AND '.join(clauses)} "  # nosec B608
            "ORDER BY t.occurred_at ASC, e.name ASC LIMIT ?",
            tuple(params),
        ).fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------
    # Outbound notification queue (the push channel drained by the bridge).
    # ------------------------------------------------------------------

    def enqueue_notification(
        self,
        user_id: str,
        chat_id: str,
        body: str,
        *,
        kind: str = "",
        dedup_key: str = "",
    ) -> bool:
        """Queue a push message. Returns False when a same dedup_key already exists."""
        with self.transaction() as conn:
            cursor = conn.execute(
                """INSERT OR IGNORE INTO outbound_notifications(
                       id, user_id, chat_id, kind, dedup_key, body, status, attempts, created_at)
                   VALUES(?, ?, ?, ?, ?, ?, 'pending', 0, ?)""",
                (new_id("notif"), user_id, str(chat_id), kind, dedup_key, body, utc_now()),
            )
        return cursor.rowcount > 0

    def list_pending_notifications(self, *, limit: int = 20, max_attempts: int = 5) -> list[dict[str, Any]]:
        rows = self.execute(
            "SELECT id, user_id, chat_id, kind, body FROM outbound_notifications "
            "WHERE status='pending' AND attempts < ? ORDER BY created_at ASC LIMIT ?",
            (max(1, int(max_attempts)), max(1, min(int(limit), 100))),
        ).fetchall()
        return [dict(row) for row in rows]

    def mark_notifications(
        self,
        sent_ids: Sequence[str] = (),
        failed_ids: Sequence[str] = (),
        *,
        max_attempts: int = 5,
    ) -> None:
        with self.transaction() as conn:
            for notif_id in sent_ids:
                conn.execute(
                    "UPDATE outbound_notifications SET status='sent', sent_at=? WHERE id=?",
                    (utc_now(), str(notif_id)),
                )
            for notif_id in failed_ids:
                # A failed send stays pending for retry until the attempt cap;
                # then it becomes a terminal 'failed' row rather than looping.
                conn.execute(
                    """UPDATE outbound_notifications
                       SET attempts=attempts+1,
                           status=CASE WHEN attempts + 1 >= ? THEN 'failed' ELSE 'pending' END
                       WHERE id=?""",
                    (max(1, int(max_attempts)), str(notif_id)),
                )

    # ------------------------------------------------------------------
    # Retrieval eval gold set.
    # ------------------------------------------------------------------

    def add_eval_case(
        self,
        user_id: str,
        query: str,
        expected_ids: Sequence[str],
        *,
        note: str = "",
        source: str = "manual",
    ) -> dict[str, Any]:
        clean_query = " ".join(str(query or "").split()).strip()
        if not clean_query:
            raise ValueError("Eval case query is required")
        ids = sorted({str(item) for item in expected_ids if str(item).strip()})
        if not ids:
            raise ValueError("At least one expected knowledge object is required")
        now = utc_now()
        with self.transaction() as conn:
            conn.execute(
                """INSERT INTO eval_cases(id, user_id, query, expected_ids_json, note, source, created_at)
                   VALUES(?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(user_id, query) DO UPDATE SET
                     expected_ids_json=excluded.expected_ids_json,
                     note=excluded.note, source=excluded.source""",
                (new_id("eval"), user_id, clean_query, json.dumps(ids), note[:500], source[:40], now),
            )
        return next((case for case in self.list_eval_cases(user_id) if case["query"] == clean_query), {})

    def list_eval_cases(self, user_id: str, *, limit: int = 1000) -> list[dict[str, Any]]:
        rows = self.execute(
            "SELECT * FROM eval_cases WHERE user_id=? ORDER BY created_at DESC LIMIT ?",
            (user_id, max(1, min(int(limit), 5000))),
        ).fetchall()
        cases = []
        for row in rows:
            case = dict(row)
            case["expected_ids"] = _json_load(case.pop("expected_ids_json", "[]"), [])
            cases.append(case)
        return cases

    def delete_eval_case(self, user_id: str, case_id: str) -> bool:
        with self.transaction() as conn:
            cursor = conn.execute("DELETE FROM eval_cases WHERE id=? AND user_id=?", (case_id, user_id))
        return cursor.rowcount > 0

    def link_knowledge_entity(
        self,
        user_id: str,
        knowledge_object_id: str,
        entity_id: str,
        *,
        status: str = "accepted",
        confidence: float = 1.0,
        evidence: dict[str, Any] | None = None,
        reviewed_by: str | None = None,
    ) -> dict[str, Any]:
        if status not in {"suggested", "accepted", "rejected"}:
            raise ValueError("status must be suggested, accepted, or rejected")
        parsed_confidence = float(confidence)
        if not math.isfinite(parsed_confidence) or not 0.0 <= parsed_confidence <= 1.0:
            raise ValueError("confidence must be a finite number between 0 and 1")
        ko = self.get_knowledge_object(knowledge_object_id, user_id)
        entity = self.get_entity(entity_id, user_id)
        if not ko or not entity or entity.get("deleted_at"):
            raise ValueError("Knowledge object and entity must belong to the same user")
        now = utc_now()
        link_id = new_id("kel")
        with self.transaction() as conn:
            conn.execute(
                """INSERT INTO knowledge_entity_links(id, user_id, knowledge_object_id, entity_id,
                   status, confidence, evidence_json, created_at, reviewed_at, reviewed_by)
                   VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(user_id, knowledge_object_id, entity_id) DO UPDATE SET
                     status=excluded.status, confidence=excluded.confidence,
                     evidence_json=excluded.evidence_json, reviewed_at=excluded.reviewed_at,
                     reviewed_by=excluded.reviewed_by""",
                (
                    link_id,
                    user_id,
                    knowledge_object_id,
                    entity_id,
                    status,
                    parsed_confidence,
                    json.dumps(evidence or {}, ensure_ascii=False, sort_keys=True),
                    now,
                    now if reviewed_by else None,
                    reviewed_by,
                ),
            )
            # Keep legacy primary link synchronized for older clients.
            if status == "accepted" and not ko.get("entity_id"):
                conn.execute(
                    "UPDATE knowledge_objects SET entity_id=?, updated_at=? WHERE id=? AND user_id=?",
                    (entity_id, now, knowledge_object_id, user_id),
                )
        row = self.execute(
            """SELECT * FROM knowledge_entity_links
               WHERE user_id=? AND knowledge_object_id=? AND entity_id=?""",
            (user_id, knowledge_object_id, entity_id),
        ).fetchone()
        return dict(row) if row else {}

    def list_knowledge_entity_links(
        self,
        user_id: str,
        *,
        entity_id: str | None = None,
        knowledge_object_id: str | None = None,
        status: str | None = "accepted",
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        clauses = ["l.user_id=?"]
        params: list[Any] = [user_id]
        if entity_id:
            clauses.append("l.entity_id=?")
            params.append(entity_id)
        if knowledge_object_id:
            clauses.append("l.knowledge_object_id=?")
            params.append(knowledge_object_id)
        if status:
            clauses.append("l.status=?")
            params.append(status)
        params.append(max(1, min(limit, 5000)))
        # ``clauses`` contains only fixed predicates; values remain bound.
        query = f"""SELECT l.*, e.name AS entity_name, e.entity_type,
                       k.title AS knowledge_title, k.lifecycle_stage AS knowledge_lifecycle
                FROM knowledge_entity_links l
                JOIN entities e ON e.id=l.entity_id AND e.user_id=l.user_id
                JOIN knowledge_objects k ON k.id=l.knowledge_object_id AND k.user_id=l.user_id
                WHERE {" AND ".join(clauses)}
                ORDER BY CASE l.status WHEN 'suggested' THEN 0 WHEN 'accepted' THEN 1 ELSE 2 END,
                         l.confidence DESC, l.created_at DESC LIMIT ?"""  # nosec B608
        rows = self.execute(query, tuple(params)).fetchall()
        return [dict(row) for row in rows]

    def set_knowledge_entity_link_status(
        self,
        link_id: str,
        user_id: str,
        status: str,
        *,
        reviewed_by: str,
    ) -> dict[str, Any] | None:
        """Review a proposed link without losing its evidence or history."""

        if status not in {"suggested", "accepted", "rejected"}:
            raise ValueError("status must be suggested, accepted, or rejected")
        link = self.execute(
            "SELECT * FROM knowledge_entity_links WHERE id=? AND user_id=?",
            (link_id, user_id),
        ).fetchone()
        if not link:
            return None
        now = utc_now()
        with self.transaction() as conn:
            conn.execute(
                """UPDATE knowledge_entity_links
                   SET status=?, reviewed_at=?, reviewed_by=?
                   WHERE id=? AND user_id=?""",
                (status, now, reviewed_by, link_id, user_id),
            )
            if status == "accepted":
                conn.execute(
                    """UPDATE knowledge_objects SET entity_id=COALESCE(entity_id, ?), updated_at=?
                       WHERE id=? AND user_id=?""",
                    (link["entity_id"], now, link["knowledge_object_id"], user_id),
                )
            else:
                current = conn.execute(
                    """SELECT entity_id FROM knowledge_objects
                       WHERE id=? AND user_id=?""",
                    (link["knowledge_object_id"], user_id),
                ).fetchone()
                if current and current["entity_id"] == link["entity_id"]:
                    fallback = conn.execute(
                        """SELECT entity_id FROM knowledge_entity_links
                           WHERE user_id=? AND knowledge_object_id=? AND status='accepted'
                             AND id<>? ORDER BY confidence DESC, created_at ASC LIMIT 1""",
                        (user_id, link["knowledge_object_id"], link_id),
                    ).fetchone()
                    conn.execute(
                        """UPDATE knowledge_objects SET entity_id=?, updated_at=?
                           WHERE id=? AND user_id=?""",
                        (
                            fallback["entity_id"] if fallback else None,
                            now,
                            link["knowledge_object_id"],
                            user_id,
                        ),
                    )
        row = self.execute(
            """SELECT l.*, e.name AS entity_name, e.entity_type, k.title AS knowledge_title
               FROM knowledge_entity_links l
               JOIN entities e ON e.id=l.entity_id AND e.user_id=l.user_id
               JOIN knowledge_objects k ON k.id=l.knowledge_object_id AND k.user_id=l.user_id
               WHERE l.id=? AND l.user_id=?""",
            (link_id, user_id),
        ).fetchone()
        return dict(row) if row else None

    def get_entity_knowledge(self, user_id: str, entity_id: str, *, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.execute(
            """SELECT k.*, l.confidence AS _link_confidence, l.evidence_json AS _link_evidence_json
               FROM knowledge_entity_links l
               JOIN knowledge_objects k ON k.id=l.knowledge_object_id
               WHERE l.user_id=? AND l.entity_id=? AND l.status='accepted' AND k.deleted_at IS NULL
               ORDER BY k.importance DESC, k.updated_at DESC LIMIT ?""",
            (user_id, entity_id, max(1, min(limit, 1000))),
        ).fetchall()
        if not rows:
            rows = self.execute(
                """SELECT * FROM knowledge_objects WHERE user_id=? AND entity_id=?
                   AND deleted_at IS NULL ORDER BY importance DESC, updated_at DESC LIMIT ?""",
                (user_id, entity_id, max(1, min(limit, 1000))),
            ).fetchall()
        return [dict(row) for row in rows]

    def create_relation(self, relation: Relation) -> Relation:
        if relation.source_entity_id == relation.target_entity_id:
            raise ValueError("Self-relations are not allowed")
        relation_weight = float(relation.weight)
        if not math.isfinite(relation_weight) or not 0.0 <= relation_weight <= 1.5:
            raise ValueError("Relation weight must be a finite number between 0 and 1.5")
        relation.weight = relation_weight
        source = self.get_entity(relation.source_entity_id, relation.user_id)
        target = self.get_entity(relation.target_entity_id, relation.user_id)
        if not source or not target or source.get("deleted_at") or target.get("deleted_at"):
            raise ValueError("Both entities must belong to the same user")
        with self.transaction() as conn:
            try:
                conn.execute(
                    """INSERT INTO relations(id, user_id, source_entity_id, target_entity_id,
                       relation_type, weight, metadata_json, created_at, deleted_at)
                       VALUES(:id, :user_id, :source_entity_id, :target_entity_id,
                       :relation_type, :weight, :metadata_json, :created_at, :deleted_at)""",
                    relation.to_row(),
                )
            except sqlite3.IntegrityError:
                row = conn.execute(
                    """SELECT id FROM relations WHERE user_id=? AND source_entity_id=?
                       AND target_entity_id=? AND relation_type=? AND deleted_at IS NULL""",
                    (
                        relation.user_id,
                        relation.source_entity_id,
                        relation.target_entity_id,
                        enum_value(relation.relation_type),
                    ),
                ).fetchone()
                if row:
                    relation.id = row["id"]
                else:
                    raise
        return relation

    def get_entity_relations(self, entity_id: str, user_id: str | None = None) -> list[dict[str, Any]]:
        params: list[Any] = [entity_id, entity_id]
        user_clause = ""
        if user_id is not None:
            user_clause = " AND r.user_id=?"
            params.append(user_id)
        # ``user_clause`` is one fixed optional predicate; the value is bound.
        query = f"""SELECT r.*, s.name AS source_name, t.name AS target_name
                FROM relations r
                JOIN entities s ON s.id=r.source_entity_id
                JOIN entities t ON t.id=r.target_entity_id
                WHERE (r.source_entity_id=? OR r.target_entity_id=?)
                  {user_clause} AND r.deleted_at IS NULL
                ORDER BY r.created_at DESC"""  # nosec B608
        rows = self.execute(query, tuple(params)).fetchall()
        return [dict(row) for row in rows]

    def get_entity_graph(self, user_id: str, entity_id: str, depth: int = 2) -> dict[str, Any]:
        root = self.get_entity(entity_id, user_id)
        if not root or root.get("deleted_at"):
            return {"nodes": [], "edges": [], "root": entity_id}
        max_depth = max(0, min(depth, 5))
        seen = {entity_id}
        frontier = {entity_id}
        nodes: dict[str, dict[str, Any]] = {entity_id: root}
        edges: dict[str, dict[str, Any]] = {}
        for _ in range(max_depth):
            next_frontier: set[str] = set()
            for current in frontier:
                for relation in self.get_entity_relations(current, user_id):
                    edges[relation["id"]] = relation
                    for candidate in (relation["source_entity_id"], relation["target_entity_id"]):
                        if candidate in seen:
                            continue
                        entity = self.get_entity(candidate, user_id)
                        if entity and not entity.get("deleted_at"):
                            seen.add(candidate)
                            nodes[candidate] = entity
                            next_frontier.add(candidate)
            frontier = next_frontier
            if not frontier:
                break
        return {"root": entity_id, "nodes": list(nodes.values()), "edges": list(edges.values())}

    def store_relation_candidate(
        self,
        user_id: str,
        source_entity_id: str,
        target_entity_id: str,
        relation_type: str,
        *,
        confidence: float,
        evidence: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create or refresh a review-only graph relation proposal."""

        if source_entity_id == target_entity_id:
            raise ValueError("Self-relation candidates are not allowed")
        source = self.get_entity(source_entity_id, user_id)
        target = self.get_entity(target_entity_id, user_id)
        if not source or not target or source.get("deleted_at") or target.get("deleted_at"):
            raise ValueError("Both candidate entities must belong to the same user")
        relation_type = str(relation_type or "related_to").strip().casefold()
        allowed_types = {item.value for item in RelationType}
        if relation_type not in allowed_types:
            raise ValueError("Unsupported relation type")
        parsed_confidence = float(confidence)
        if not math.isfinite(parsed_confidence) or not 0.0 <= parsed_confidence <= 1.0:
            raise ValueError("confidence must be a finite number between 0 and 1")
        candidate_id = new_id("relc")
        now = utc_now()
        with self.transaction() as conn:
            conn.execute(
                """INSERT INTO relation_candidates(
                       id, user_id, source_entity_id, target_entity_id, relation_type,
                       confidence, evidence_json, status, created_at
                   ) VALUES(?, ?, ?, ?, ?, ?, ?, 'suggested', ?)
                   ON CONFLICT(user_id, source_entity_id, target_entity_id, relation_type) DO UPDATE SET
                     confidence=MAX(relation_candidates.confidence, excluded.confidence),
                     evidence_json=CASE
                       WHEN excluded.confidence >= relation_candidates.confidence THEN excluded.evidence_json
                       ELSE relation_candidates.evidence_json
                     END,
                     status=CASE
                       WHEN relation_candidates.status IN ('accepted', 'rejected')
                         THEN relation_candidates.status
                       ELSE 'suggested'
                     END""",
                (
                    candidate_id,
                    user_id,
                    source_entity_id,
                    target_entity_id,
                    relation_type,
                    parsed_confidence,
                    json.dumps(evidence or {}, ensure_ascii=False, sort_keys=True),
                    now,
                ),
            )
        row = self.execute(
            """SELECT c.*, s.name AS source_name, t.name AS target_name
               FROM relation_candidates c
               JOIN entities s ON s.id=c.source_entity_id AND s.user_id=c.user_id
               JOIN entities t ON t.id=c.target_entity_id AND t.user_id=c.user_id
               WHERE c.user_id=? AND c.source_entity_id=? AND c.target_entity_id=?
                 AND c.relation_type=?""",
            (user_id, source_entity_id, target_entity_id, relation_type),
        ).fetchone()
        return dict(row) if row else {}

    def get_relation_candidate(self, user_id: str, candidate_id: str) -> dict[str, Any] | None:
        row = self.execute(
            """SELECT c.*, s.name AS source_name, s.entity_type AS source_type,
                      t.name AS target_name, t.entity_type AS target_type
               FROM relation_candidates c
               JOIN entities s ON s.id=c.source_entity_id AND s.user_id=c.user_id
               JOIN entities t ON t.id=c.target_entity_id AND t.user_id=c.user_id
               WHERE c.id=? AND c.user_id=?""",
            (candidate_id, user_id),
        ).fetchone()
        return dict(row) if row else None

    def list_relation_candidates(
        self,
        user_id: str,
        *,
        status: str | None = "suggested",
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        clauses = ["c.user_id=?"]
        params: list[Any] = [user_id]
        if status:
            if status not in {"suggested", "accepted", "rejected"}:
                raise ValueError("Invalid relation candidate status")
            clauses.append("c.status=?")
            params.append(status)
        params.append(max(1, min(int(limit), 5000)))
        # ``clauses`` contains only fixed predicates; values remain bound.
        query = f"""SELECT c.*, s.name AS source_name, s.entity_type AS source_type,
                       t.name AS target_name, t.entity_type AS target_type
                FROM relation_candidates c
                JOIN entities s ON s.id=c.source_entity_id AND s.user_id=c.user_id
                JOIN entities t ON t.id=c.target_entity_id AND t.user_id=c.user_id
                WHERE {" AND ".join(clauses)}
                ORDER BY c.confidence DESC, c.created_at DESC LIMIT ?"""  # nosec B608
        rows = self.execute(query, tuple(params)).fetchall()
        return [dict(row) for row in rows]

    def review_relation_candidate(
        self,
        user_id: str,
        candidate_id: str,
        status: str,
        *,
        reviewed_by: str,
    ) -> dict[str, Any] | None:
        if status not in {"accepted", "rejected"}:
            raise ValueError("status must be accepted or rejected")
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM relation_candidates WHERE id=? AND user_id=?",
                (candidate_id, user_id),
            ).fetchone()
            if not row:
                return None
            current_status = str(row["status"] or "suggested")
            if current_status in {"accepted", "rejected"}:
                if current_status != status:
                    raise ValueError(
                        f"Relation candidate is already {current_status}; reviewed decisions are terminal"
                    )
            else:
                now = utc_now()
                conn.execute(
                    """UPDATE relation_candidates SET status=?, reviewed_at=?, reviewed_by=?
                       WHERE id=? AND user_id=?""",
                    (status, now, reviewed_by, candidate_id, user_id),
                )
                if status == "accepted":
                    relation = Relation(
                        id=new_id("rel"),
                        user_id=user_id,
                        source_entity_id=str(row["source_entity_id"]),
                        target_entity_id=str(row["target_entity_id"]),
                        relation_type=str(row["relation_type"]),
                        weight=max(0.1, min(1.0, float(row["confidence"] or 0.5))),
                        metadata_json={
                            "origin": "review",
                            "source": "reviewed_relation_candidate",
                            "candidate_id": candidate_id,
                            "reviewed_by": reviewed_by,
                            # weight is clamped for ranking; the extractor's raw
                            # confidence stays available as edge provenance.
                            "confidence": float(row["confidence"] or 0.5),
                            "evidence": _json_load(row["evidence_json"], {}),
                        },
                    )
                    # Accepting an already represented relation remains idempotent.
                    with suppress(sqlite3.IntegrityError):
                        conn.execute(
                            """INSERT INTO relations(id, user_id, source_entity_id, target_entity_id,
                               relation_type, weight, metadata_json, created_at, deleted_at)
                               VALUES(:id, :user_id, :source_entity_id, :target_entity_id,
                               :relation_type, :weight, :metadata_json, :created_at, :deleted_at)""",
                            relation.to_row(),
                        )
        return self.get_relation_candidate(user_id, candidate_id)

    @staticmethod
    def _conflict_pair_key(knowledge_a_id: str, knowledge_b_id: str) -> str:
        return "|".join(sorted((knowledge_a_id, knowledge_b_id)))

    def store_knowledge_conflict(
        self,
        user_id: str,
        knowledge_a_id: str,
        knowledge_b_id: str,
        *,
        conflict_type: str = "potential_contradiction",
        confidence: float,
        evidence: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if knowledge_a_id == knowledge_b_id:
            raise ValueError("A knowledge object cannot conflict with itself")
        left = self.get_knowledge_object(knowledge_a_id, user_id)
        right = self.get_knowledge_object(knowledge_b_id, user_id)
        if not left or not right or left.get("deleted_at") or right.get("deleted_at"):
            raise ValueError("Both knowledge objects must belong to the same user")
        parsed_confidence = float(confidence)
        if not math.isfinite(parsed_confidence) or not 0.0 <= parsed_confidence <= 1.0:
            raise ValueError("confidence must be a finite number between 0 and 1")
        pair_key = self._conflict_pair_key(knowledge_a_id, knowledge_b_id)
        conflict_id = new_id("conf")
        now = utc_now()
        with self.transaction() as conn:
            conn.execute(
                """INSERT INTO knowledge_conflicts(
                       id, user_id, knowledge_a_id, knowledge_b_id, pair_key,
                       conflict_type, confidence, evidence_json, status, created_at
                   ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, 'suggested', ?)
                   ON CONFLICT(user_id, pair_key, conflict_type) DO UPDATE SET
                     confidence=MAX(knowledge_conflicts.confidence, excluded.confidence),
                     evidence_json=CASE
                       WHEN excluded.confidence >= knowledge_conflicts.confidence THEN excluded.evidence_json
                       ELSE knowledge_conflicts.evidence_json
                     END""",
                (
                    conflict_id,
                    user_id,
                    knowledge_a_id,
                    knowledge_b_id,
                    pair_key,
                    str(conflict_type or "potential_contradiction")[:80],
                    parsed_confidence,
                    json.dumps(evidence or {}, ensure_ascii=False, sort_keys=True),
                    now,
                ),
            )
        rows = self.list_knowledge_conflicts(user_id, status=None, limit=5000)
        return next((item for item in rows if item["pair_key"] == pair_key), {})

    def list_knowledge_conflicts(
        self,
        user_id: str,
        *,
        status: str | None = "suggested",
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        allowed = {"suggested", "confirmed", "dismissed", "resolved"}
        clauses = ["c.user_id=?"]
        params: list[Any] = [user_id]
        if status:
            if status not in allowed:
                raise ValueError("Invalid conflict status")
            clauses.append("c.status=?")
            params.append(status)
        params.append(max(1, min(int(limit), 5000)))
        # ``clauses`` contains only fixed predicates; values remain bound.
        query = f"""SELECT c.*, a.title AS knowledge_a_title, a.summary AS knowledge_a_summary,
                       b.title AS knowledge_b_title, b.summary AS knowledge_b_summary
                FROM knowledge_conflicts c
                JOIN knowledge_objects a ON a.id=c.knowledge_a_id AND a.user_id=c.user_id
                JOIN knowledge_objects b ON b.id=c.knowledge_b_id AND b.user_id=c.user_id
                WHERE {" AND ".join(clauses)}
                ORDER BY c.confidence DESC, c.created_at DESC LIMIT ?"""  # nosec B608
        rows = self.execute(query, tuple(params)).fetchall()
        return [dict(row) for row in rows]

    def get_knowledge_conflict(self, user_id: str, conflict_id: str) -> dict[str, Any] | None:
        row = self.execute(
            """SELECT c.*, a.title AS knowledge_a_title, a.summary AS knowledge_a_summary,
                      b.title AS knowledge_b_title, b.summary AS knowledge_b_summary
               FROM knowledge_conflicts c
               JOIN knowledge_objects a ON a.id=c.knowledge_a_id AND a.user_id=c.user_id
               JOIN knowledge_objects b ON b.id=c.knowledge_b_id AND b.user_id=c.user_id
               WHERE c.id=? AND c.user_id=?""",
            (conflict_id, user_id),
        ).fetchone()
        return dict(row) if row else None

    def review_knowledge_conflict(
        self,
        user_id: str,
        conflict_id: str,
        status: str,
        *,
        reviewed_by: str,
        resolution_note: str = "",
    ) -> dict[str, Any] | None:
        if status not in {"confirmed", "dismissed", "resolved"}:
            raise ValueError("Invalid conflict review status")
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT status FROM knowledge_conflicts WHERE id=? AND user_id=?",
                (conflict_id, user_id),
            ).fetchone()
            if row is None:
                return None
            current_status = str(row["status"] or "suggested")
            if current_status == status:
                pass
            elif current_status == "suggested" or (current_status == "confirmed" and status == "resolved"):
                conn.execute(
                    """UPDATE knowledge_conflicts
                       SET status=?, reviewed_at=?, reviewed_by=?, resolution_note=?
                       WHERE id=? AND user_id=?""",
                    (status, utc_now(), reviewed_by, resolution_note[:2000], conflict_id, user_id),
                )
            else:
                raise ValueError(
                    f"Conflict is already {current_status}; only confirmed conflicts may advance to resolved"
                )
        return self.get_knowledge_conflict(user_id, conflict_id)

    def resolve_conflict(
        self,
        user_id: str,
        conflict_id: str,
        winner_id: str,
        *,
        reviewed_by: str,
        resolution_note: str = "",
    ) -> dict[str, Any] | None:
        """Resolve a conflict by choosing a winner; the loser is deprecated.

        Detection and confirmation only flag a contradiction — this is the
        action that actually settles it: the losing Knowledge Object becomes
        ``deprecated`` and points at the winner (``superseded_by_id`` plus a
        ``deprecated_by_conflict`` metadata stamp), and the conflict is marked
        ``resolved``. Provenance is preserved: the loser is versioned, not
        deleted, and can be reactivated by editing it. Ordering (deprecate the
        loser, then flip the conflict) keeps a re-run after a crash idempotent.
        """
        conflict = self.get_knowledge_conflict(user_id, conflict_id)
        if conflict is None:
            return None
        knowledge_a = str(conflict["knowledge_a_id"])
        knowledge_b = str(conflict["knowledge_b_id"])
        if winner_id not in (knowledge_a, knowledge_b):
            raise ValueError("winner_id must be one of the conflicting knowledge objects")
        current_status = str(conflict.get("status") or "suggested")
        if current_status in {"dismissed", "resolved"}:
            raise ValueError(f"Conflict is already {current_status}")
        loser_id = knowledge_b if winner_id == knowledge_a else knowledge_a
        loser = self.get_knowledge_object(loser_id, user_id)
        if loser is None or loser.get("deleted_at"):
            raise ValueError("Losing knowledge object not found")

        metadata = _json_load(loser.get("metadata_json"), {})
        if not isinstance(metadata, dict):
            metadata = {}
        metadata["deprecated_by_conflict"] = {
            "conflict_id": conflict_id,
            "superseded_by": winner_id,
            "at": utc_now(),
        }
        self.update_knowledge_fields(
            loser_id,
            user_id,
            lifecycle_stage=LifecycleStage.DEPRECATED.value,
            superseded_by_id=winner_id,
            metadata_json=metadata,
        )
        note = (resolution_note or f"kept {winner_id}; deprecated {loser_id}")[:2000]
        self.review_knowledge_conflict(
            user_id, conflict_id, "resolved", reviewed_by=reviewed_by, resolution_note=note
        )
        return {
            "conflict": self.get_knowledge_conflict(user_id, conflict_id),
            "winner_id": winner_id,
            "deprecated_id": loser_id,
        }

    def find_duplicate_candidates(
        self,
        user_id: str,
        *,
        min_confidence: float = 0.5,
    ) -> list[EntityResolutionCandidate]:
        """Generate conservative, context-aware duplicate proposals.

        Name similarity is only one signal.  Shared Knowledge Objects and graph neighbours raise
        confidence, while compact identifiers never use fuzzy or prefix matching.  The method only
        creates review candidates; it never performs a merge.
        """

        entities = self.list_entities(user_id, limit=5000)
        knowledge_by_entity: dict[str, set[str]] = {}
        for row in self.execute(
            """SELECT entity_id, knowledge_object_id FROM knowledge_entity_links
               WHERE user_id=? AND status='accepted'""",
            (user_id,),
        ).fetchall():
            knowledge_by_entity.setdefault(str(row["entity_id"]), set()).add(str(row["knowledge_object_id"]))
        neighbours_by_entity: dict[str, set[str]] = {}
        for row in self.execute(
            """SELECT source_entity_id, target_entity_id FROM relations
               WHERE user_id=? AND deleted_at IS NULL""",
            (user_id,),
        ).fetchall():
            source = str(row["source_entity_id"])
            target = str(row["target_entity_id"])
            neighbours_by_entity.setdefault(source, set()).add(target)
            neighbours_by_entity.setdefault(target, set()).add(source)

        def variants(entity: dict[str, Any]) -> list[str]:
            values = [str(entity.get("name") or "")]
            values.extend(str(item) for item in _json_load(entity.get("aliases_json"), []))
            return [normalize_entity_name(value) for value in values if normalize_entity_name(value)]

        def acronym(name: str) -> str:
            tokens = [token for token in re.split(r"\s+", name) if token]
            return "".join(token[0] for token in tokens if token).casefold() if len(tokens) >= 2 else ""

        def overlap(left: set[str], right: set[str]) -> float:
            union = left | right
            return len(left & right) / len(union) if union else 0.0

        candidates: list[EntityResolutionCandidate] = []
        min_confidence = max(0.0, min(1.0, float(min_confidence)))
        prepared: list[dict[str, Any]] = [
            {
                "entity": entity,
                "variants": variants(entity),
                "identifier": any(
                    _is_entity_identifier(str(value))
                    for value in [entity.get("name", ""), *_json_load(entity.get("aliases_json"), [])]
                ),
            }
            for entity in entities
        ]
        for index, left_data in enumerate(prepared):
            left = left_data["entity"]
            left_variants = left_data["variants"]
            left_name = left_variants[0] if left_variants else ""
            for right_data in prepared[index + 1 :]:
                right = right_data["entity"]
                if left["entity_type"] != right["entity_type"]:
                    continue
                right_variants = right_data["variants"]
                right_name = right_variants[0] if right_variants else ""
                if not left_name or not right_name:
                    continue

                exact_alias = bool(set(left_variants) & set(right_variants))
                # Codes, tickers, contract identifiers, and versioned names are exact-match only.
                if (left_data["identifier"] or right_data["identifier"]) and not exact_alias:
                    continue

                name_similarity = SequenceMatcher(None, left_name, right_name).ratio()
                left_tokens = set(left_name.split())
                right_tokens = set(right_name.split())
                token_jaccard = overlap(left_tokens, right_tokens)
                sorted_similarity = SequenceMatcher(
                    None,
                    " ".join(sorted(left_tokens)),
                    " ".join(sorted(right_tokens)),
                ).ratio()
                alias_similarity = max(
                    (
                        SequenceMatcher(None, left_variant, right_variant).ratio()
                        for left_variant in left_variants
                        for right_variant in right_variants
                    ),
                    default=0.0,
                )
                acronym_match = bool(
                    acronym(left_name)
                    and acronym(left_name) == acronym(right_name)
                    and len(left_tokens) >= 2
                    and len(right_tokens) >= 2
                )
                shared_knowledge = overlap(
                    knowledge_by_entity.get(str(left["id"]), set()),
                    knowledge_by_entity.get(str(right["id"]), set()),
                )
                shared_neighbours = overlap(
                    neighbours_by_entity.get(str(left["id"]), set()),
                    neighbours_by_entity.get(str(right["id"]), set()),
                )

                if exact_alias:
                    confidence = 0.995
                    method = "exact_name_or_alias"
                elif left_tokens == right_tokens and len(left_tokens) >= 2:
                    confidence = 0.94
                    method = "same_tokens_different_order"
                else:
                    confidence = max(
                        name_similarity * 0.70,
                        sorted_similarity * 0.78,
                        token_jaccard * 0.90,
                        alias_similarity * 0.76,
                        0.82 if acronym_match else 0.0,
                    )
                    context_boost = min(0.14, shared_knowledge * 0.09 + shared_neighbours * 0.07)
                    confidence = min(0.97, confidence + context_boost)
                    method = "name_alias_and_graph_evidence"

                # A single generic token needs very strong evidence; fuzzy short names create noise.
                if len(left_tokens) == len(right_tokens) == 1 and not exact_alias:
                    if min(len(left_name), len(right_name)) < 5:
                        confidence *= 0.72
                    if shared_knowledge == 0 and shared_neighbours == 0:
                        confidence *= 0.88
                if confidence < min_confidence:
                    continue
                candidates.append(
                    EntityResolutionCandidate(
                        id=new_id("er"),
                        user_id=user_id,
                        entity_a_id=left["id"],
                        entity_b_id=right["id"],
                        confidence=round(confidence, 6),
                        resolution_method=method,
                        evidence_json={
                            "left_name": left["name"],
                            "right_name": right["name"],
                            "name_similarity": round(name_similarity, 4),
                            "sorted_token_similarity": round(sorted_similarity, 4),
                            "token_jaccard": round(token_jaccard, 4),
                            "alias_similarity": round(alias_similarity, 4),
                            "exact_alias": exact_alias,
                            "acronym_match": acronym_match,
                            "shared_knowledge": round(shared_knowledge, 4),
                            "shared_graph_neighbours": round(shared_neighbours, 4),
                            "identifier_safe": not (left_data["identifier"] or right_data["identifier"]),
                        },
                    )
                )
        candidates.sort(key=lambda item: item.confidence, reverse=True)
        return candidates

    def store_resolution_candidate(self, candidate: EntityResolutionCandidate) -> EntityResolutionCandidate:
        if candidate.entity_a_id == candidate.entity_b_id:
            raise ValueError("A resolution candidate must contain two distinct entities")
        left = self.get_entity(candidate.entity_a_id, candidate.user_id)
        right = self.get_entity(candidate.entity_b_id, candidate.user_id)
        if not left or not right:
            raise ValueError("Resolution entities must belong to the same user")
        row = candidate.to_row()
        with self.transaction() as conn:
            existing = conn.execute(
                "SELECT * FROM entity_resolution_candidates WHERE user_id=? AND pair_key=?",
                (candidate.user_id, candidate.pair_key),
            ).fetchone()
            if existing:
                # Rejections and completed decisions are durable.  Pending candidates may receive
                # stronger evidence over time without changing their identity or review state.
                existing_candidate = self._resolution_from_row(existing)
                if str(existing_candidate.status) == ResolutionStatus.SUGGESTED.value and float(
                    candidate.confidence
                ) > float(existing_candidate.confidence):
                    conn.execute(
                        """UPDATE entity_resolution_candidates
                           SET confidence=?, resolution_method=?, evidence_json=?
                           WHERE id=? AND user_id=? AND status='suggested'""",
                        (
                            max(0.0, min(1.0, float(candidate.confidence))),
                            candidate.resolution_method,
                            json.dumps(candidate.evidence_json, ensure_ascii=False, sort_keys=True),
                            existing_candidate.id,
                            candidate.user_id,
                        ),
                    )
                    refreshed = conn.execute(
                        "SELECT * FROM entity_resolution_candidates WHERE id=? AND user_id=?",
                        (existing_candidate.id, candidate.user_id),
                    ).fetchone()
                    return self._resolution_from_row(refreshed) if refreshed else existing_candidate
                return existing_candidate
            conn.execute(
                """INSERT INTO entity_resolution_candidates(id, user_id, entity_a_id, entity_b_id,
                   pair_key, confidence, resolution_method, evidence_json, status, resolved_by,
                   created_at, resolved_at)
                   VALUES(:id, :user_id, :entity_a_id, :entity_b_id, :pair_key, :confidence,
                   :resolution_method, :evidence_json, :status, :resolved_by, :created_at, :resolved_at)""",
                row,
            )
        return candidate

    @staticmethod
    def _resolution_from_row(row: sqlite3.Row | dict[str, Any]) -> EntityResolutionCandidate:
        data = dict(row)
        return EntityResolutionCandidate(
            id=data["id"],
            user_id=data["user_id"],
            entity_a_id=data["entity_a_id"],
            entity_b_id=data["entity_b_id"],
            confidence=float(data.get("confidence", 0.0)),
            resolution_method=data.get("resolution_method", "name_similarity"),
            evidence_json=_json_load(data.get("evidence_json"), {}),
            status=data.get("status", "suggested"),
            resolved_by=data.get("resolved_by"),
            created_at=data.get("created_at", utc_now()),
            resolved_at=data.get("resolved_at"),
        )

    def get_resolution_candidate(self, candidate_id: str, user_id: str) -> dict[str, Any] | None:
        row = self.execute(
            "SELECT * FROM entity_resolution_candidates WHERE id=? AND user_id=?",
            (candidate_id, user_id),
        ).fetchone()
        return dict(row) if row else None

    def list_resolution_candidates(
        self,
        user_id: str,
        status: ResolutionStatus | None = None,
    ) -> list[dict[str, Any]]:
        if status:
            rows = self.execute(
                """SELECT * FROM entity_resolution_candidates WHERE user_id=? AND status=?
                   ORDER BY confidence DESC, created_at DESC""",
                (user_id, enum_value(status)),
            ).fetchall()
        else:
            rows = self.execute(
                "SELECT * FROM entity_resolution_candidates WHERE user_id=? ORDER BY confidence DESC, created_at DESC",
                (user_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def resolve_candidate(
        self,
        candidate_id: str,
        status: ResolutionStatus,
        resolved_by: str | None = None,
        *,
        user_id: str | None = None,
    ) -> bool:
        query = "UPDATE entity_resolution_candidates SET status=?, resolved_at=?, resolved_by=? WHERE id=?"
        params: tuple[Any, ...] = (
            enum_value(status),
            None if status == ResolutionStatus.SUGGESTED else utc_now(),
            resolved_by,
            candidate_id,
        )
        if user_id is not None:
            query += " AND user_id=?"
            params += (user_id,)
        with self.transaction() as conn:
            cursor = conn.execute(query, params)
        return cursor.rowcount > 0

    def merge_entities(
        self,
        user_id: str,
        source_id: str,
        target_id: str,
        *,
        merged_by: str | None = None,
    ) -> dict[str, Any]:
        if source_id == target_id:
            raise ValueError("Cannot merge an entity into itself")
        source = self.get_entity(source_id, user_id)
        target = self.get_entity(target_id, user_id)
        if not source or not target or source.get("deleted_at") or target.get("deleted_at"):
            raise ValueError("Both canonical entities must belong to the same user")

        source_aliases = _json_load(source.get("aliases_json"), [])
        target_aliases = _json_load(target.get("aliases_json"), [])
        aliases = {
            item.strip()
            for item in [*source_aliases, *target_aliases, source["name"]]
            if item and item.strip() and normalize_entity_name(item) != normalize_entity_name(target["name"])
        }
        now = utc_now()
        target_after = dict(target)
        target_after["aliases_json"] = json.dumps(sorted(aliases, key=str.casefold), ensure_ascii=False)
        target_after["version"] = int(target.get("version", 1)) + 1
        target_after["updated_at"] = now

        with self.transaction() as conn:
            conn.execute(
                """UPDATE entities SET aliases_json=?, version=?, updated_at=?
                   WHERE id=? AND user_id=?""",
                (target_after["aliases_json"], target_after["version"], now, target_id, user_id),
            )
            self._store_entity_version(conn, target_after)

            source_links = conn.execute(
                "SELECT * FROM knowledge_entity_links WHERE user_id=? AND entity_id=?",
                (user_id, source_id),
            ).fetchall()
            for link_row in source_links:
                link = dict(link_row)
                conn.execute(
                    """INSERT OR IGNORE INTO knowledge_entity_links
                       (id, user_id, knowledge_object_id, entity_id, status, confidence,
                        evidence_json, created_at, reviewed_at, reviewed_by)
                       VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        new_id("kel"),
                        user_id,
                        link["knowledge_object_id"],
                        target_id,
                        link["status"],
                        link["confidence"],
                        link["evidence_json"],
                        link["created_at"],
                        link["reviewed_at"],
                        link["reviewed_by"],
                    ),
                )
            conn.execute(
                "DELETE FROM knowledge_entity_links WHERE user_id=? AND entity_id=?",
                (user_id, source_id),
            )
            conn.execute(
                "UPDATE knowledge_objects SET entity_id=?, updated_at=? WHERE user_id=? AND entity_id=?",
                (target_id, now, user_id, source_id),
            )

            relations = conn.execute(
                """SELECT * FROM relations WHERE user_id=? AND deleted_at IS NULL
                   AND (source_entity_id=? OR target_entity_id=?)""",
                (user_id, source_id, source_id),
            ).fetchall()
            for relation_row in relations:
                relation = dict(relation_row)
                conn.execute("DELETE FROM relations WHERE id=?", (relation["id"],))
                new_source = (
                    target_id if relation["source_entity_id"] == source_id else relation["source_entity_id"]
                )
                new_target = (
                    target_id if relation["target_entity_id"] == source_id else relation["target_entity_id"]
                )
                if new_source == new_target:
                    continue
                conn.execute(
                    """INSERT OR IGNORE INTO relations(id, user_id, source_entity_id, target_entity_id,
                       relation_type, weight, metadata_json, created_at, deleted_at)
                       VALUES(?, ?, ?, ?, ?, ?, ?, ?, NULL)""",
                    (
                        relation["id"],
                        user_id,
                        new_source,
                        new_target,
                        relation["relation_type"],
                        relation["weight"],
                        relation["metadata_json"],
                        relation["created_at"],
                    ),
                )

            conn.execute(
                """UPDATE entities SET merged_into_id=?, canonical=0, deleted_at=?, updated_at=?
                   WHERE id=? AND user_id=?""",
                (target_id, now, now, source_id, user_id),
            )
            conn.execute(
                """UPDATE entity_resolution_candidates SET status='merged', resolved_at=?, resolved_by=?
                   WHERE user_id=? AND status='suggested'
                     AND (entity_a_id=? OR entity_b_id=?)""",
                (now, merged_by or user_id, user_id, source_id, source_id),
            )
            conn.execute(
                """INSERT INTO entity_merge_history(id, user_id, source_entity_id, target_entity_id,
                   source_snapshot_json, target_before_json, target_after_json, merged_by, created_at)
                   VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    new_id("merge"),
                    user_id,
                    source_id,
                    target_id,
                    _snapshot(source),
                    _snapshot(target),
                    _snapshot(target_after),
                    merged_by or user_id,
                    now,
                ),
            )
        return self.get_entity(target_id, user_id) or {}

    def list_merge_history(self, user_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
        rows = self.execute(
            "SELECT * FROM entity_merge_history WHERE user_id=? ORDER BY created_at DESC LIMIT ?",
            (user_id, max(1, min(limit, 1000))),
        ).fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------
    # Feedback, conversations, and audit
    # ------------------------------------------------------------------

    def store_feedback(self, feedback: FeedbackItem) -> FeedbackItem:
        self.ensure_user(feedback.user_id)
        row = feedback.to_row()
        score = float(row["score"])
        if not math.isfinite(score):
            raise ValueError("feedback score must be finite")
        with self.transaction() as conn:
            previous_state = conn.execute(
                """SELECT score, context_json FROM feedback_state
                   WHERE user_id=? AND target_type=? AND target_id=? AND feedback_type=?""",
                (
                    row["user_id"],
                    row["target_type"],
                    row["target_id"],
                    row["feedback_type"],
                ),
            ).fetchone()
            conn.execute(
                """INSERT INTO feedback(id, user_id, target_type, target_id, feedback_type,
                   score, comment, context_json, created_at)
                   VALUES(:id, :user_id, :target_type, :target_id, :feedback_type,
                   :score, :comment, :context_json, :created_at)""",
                row,
            )
            conn.execute(
                """INSERT INTO feedback_state(
                       user_id, target_type, target_id, feedback_type, score,
                       comment, context_json, feedback_id, updated_at
                   ) VALUES(:user_id, :target_type, :target_id, :feedback_type, :score,
                            :comment, :context_json, :id, :created_at)
                   ON CONFLICT(user_id, target_type, target_id, feedback_type) DO UPDATE SET
                     score=excluded.score,
                     comment=excluded.comment,
                     context_json=excluded.context_json,
                     feedback_id=excluded.feedback_id,
                     updated_at=excluded.updated_at""",
                row,
            )

            context = _json_load(row["context_json"], {})
            knowledge_ids = context.get("knowledge_object_ids") if isinstance(context, dict) else []
            current_ids = (
                list(dict.fromkeys(str(item) for item in knowledge_ids if str(item).strip()))
                if isinstance(knowledge_ids, list)
                else []
            )
            previous_context = _json_load(previous_state["context_json"], {}) if previous_state else {}
            previous_ids_value = (
                previous_context.get("knowledge_object_ids") if isinstance(previous_context, dict) else []
            )
            previous_ids = (
                list(dict.fromkeys(str(item) for item in previous_ids_value if str(item).strip()))
                if isinstance(previous_ids_value, list)
                else []
            )
            previous_score = float(previous_state["score"] or 0.0) if previous_state else 0.0

            # Undo the prior current-state attribution before applying the new
            # one. The append-only feedback table remains untouched.
            for knowledge_id in previous_ids:
                conn.execute(
                    """UPDATE knowledge_usage SET
                         positive_feedback_count=MAX(0, positive_feedback_count-?),
                         negative_feedback_count=MAX(0, negative_feedback_count-?),
                         updated_at=?
                       WHERE user_id=? AND knowledge_object_id=?""",
                    (
                        1 if previous_score > 0 else 0,
                        1 if previous_score < 0 else 0,
                        row["created_at"],
                        feedback.user_id,
                        knowledge_id,
                    ),
                )

            for knowledge_id in current_ids:
                owner = conn.execute(
                    "SELECT 1 FROM knowledge_objects WHERE id=? AND user_id=?",
                    (knowledge_id, feedback.user_id),
                ).fetchone()
                if not owner:
                    continue
                positive = 1 if score > 0 else 0
                negative = 1 if score < 0 else 0
                conn.execute(
                    """INSERT INTO knowledge_usage(
                           user_id, knowledge_object_id, positive_feedback_count,
                           negative_feedback_count, last_feedback_at, updated_at
                       ) VALUES(?, ?, ?, ?, ?, ?)
                       ON CONFLICT(user_id, knowledge_object_id) DO UPDATE SET
                         positive_feedback_count=knowledge_usage.positive_feedback_count+excluded.positive_feedback_count,
                         negative_feedback_count=knowledge_usage.negative_feedback_count+excluded.negative_feedback_count,
                         last_feedback_at=excluded.last_feedback_at,
                         updated_at=excluded.updated_at""",
                    (
                        feedback.user_id,
                        knowledge_id,
                        positive,
                        negative,
                        row["created_at"],
                        row["created_at"],
                    ),
                )
        return feedback

    def get_feedback_for_target(self, user_id: str, target_type: str, target_id: str) -> list[dict[str, Any]]:
        rows = self.execute(
            """SELECT * FROM feedback WHERE user_id=? AND target_type=? AND target_id=?
               ORDER BY created_at DESC""",
            (user_id, target_type, target_id),
        ).fetchall()
        return [dict(row) for row in rows]

    def get_feedback_state(
        self,
        user_id: str,
        *,
        target_type: str | None = None,
        target_id: str | None = None,
        feedback_type: str | None = None,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        clauses = ["user_id=?"]
        params: list[Any] = [user_id]
        if target_type:
            clauses.append("target_type=?")
            params.append(target_type)
        if target_id:
            clauses.append("target_id=?")
            params.append(target_id)
        if feedback_type:
            clauses.append("feedback_type=?")
            params.append(feedback_type)
        params.append(max(1, min(int(limit), 5000)))
        # ``clauses`` contains only fixed predicates; values remain bound.
        query = f"""SELECT * FROM feedback_state WHERE {" AND ".join(clauses)}
                ORDER BY updated_at DESC LIMIT ?"""  # nosec B608
        rows = self.execute(query, tuple(params)).fetchall()
        return [dict(row) for row in rows]

    def record_knowledge_usage(
        self,
        user_id: str,
        knowledge_object_ids: list[str],
        *,
        retrieved: bool = False,
        used_in_answer: bool = False,
    ) -> int:
        """Record bounded, tenant-checked usage signals for ranking and lifecycle.

        Counts are deliberately coarse.  They improve usefulness over time
        without creating an opaque behavioral profile or allowing another
        tenant to influence a user's ranking.
        """

        unique_ids = list(dict.fromkeys(str(item) for item in knowledge_object_ids if str(item).strip()))
        if not unique_ids or (not retrieved and not used_in_answer):
            return 0
        now = utc_now()
        changed = 0
        with self.transaction() as conn:
            for knowledge_id in unique_ids[:500]:
                exists = conn.execute(
                    "SELECT 1 FROM knowledge_objects WHERE id=? AND user_id=? AND deleted_at IS NULL",
                    (knowledge_id, user_id),
                ).fetchone()
                if not exists:
                    continue
                conn.execute(
                    """INSERT INTO knowledge_usage(
                           user_id, knowledge_object_id, retrieval_count, answer_count,
                           last_retrieved_at, last_used_at, updated_at
                       ) VALUES(?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(user_id, knowledge_object_id) DO UPDATE SET
                         retrieval_count=knowledge_usage.retrieval_count+excluded.retrieval_count,
                         answer_count=knowledge_usage.answer_count+excluded.answer_count,
                         last_retrieved_at=COALESCE(excluded.last_retrieved_at, knowledge_usage.last_retrieved_at),
                         last_used_at=COALESCE(excluded.last_used_at, knowledge_usage.last_used_at),
                         updated_at=excluded.updated_at""",
                    (
                        user_id,
                        knowledge_id,
                        1 if retrieved else 0,
                        1 if used_in_answer else 0,
                        now if retrieved else None,
                        now if used_in_answer else None,
                        now,
                    ),
                )
                changed += 1
        return changed

    def get_knowledge_usage(self, user_id: str, knowledge_object_ids: list[str]) -> dict[str, dict[str, Any]]:
        output: dict[str, dict[str, Any]] = {}
        unique_ids = list(dict.fromkeys(str(item) for item in knowledge_object_ids if str(item).strip()))
        for start in range(0, len(unique_ids), 400):
            chunk = unique_ids[start : start + 400]
            if not chunk:
                continue
            placeholders = ",".join("?" for _ in chunk)
            # The only interpolated fragment is a bounded sequence of ``?`` placeholders.
            query = f"""SELECT * FROM knowledge_usage
                    WHERE user_id=? AND knowledge_object_id IN ({placeholders})"""  # nosec B608
            rows = self.execute(query, (user_id, *chunk)).fetchall()
            output.update({str(row["knowledge_object_id"]): dict(row) for row in rows})
        return output

    def get_feedback_stats(self, user_id: str, target_type: str | None = None) -> dict[str, Any]:
        if target_type:
            rows = self.execute(
                """SELECT feedback_type, AVG(score) AS avg_score, COUNT(*) AS count
                   FROM feedback WHERE user_id=? AND target_type=? GROUP BY feedback_type""",
                (user_id, target_type),
            ).fetchall()
        else:
            rows = self.execute(
                """SELECT feedback_type, AVG(score) AS avg_score, COUNT(*) AS count
                   FROM feedback WHERE user_id=? GROUP BY feedback_type""",
                (user_id,),
            ).fetchall()
        return {row["feedback_type"]: {"avg_score": row["avg_score"], "count": row["count"]} for row in rows}

    def get_current_feedback_stats(
        self,
        user_id: str,
        target_type: str | None = None,
    ) -> dict[str, Any]:
        """Summarize only each target's current feedback state.

        ``feedback`` is intentionally append-only for audit/history.  Product
        behavior, prompts, and ranking must use ``feedback_state`` so replacing
        a thumbs-up with a thumbs-down does not leave a misleading neutral
        average in the active feedback loop.
        """

        if target_type:
            rows = self.execute(
                """SELECT feedback_type, AVG(score) AS avg_score, COUNT(*) AS count,
                          SUM(CASE WHEN score > 0 THEN 1 ELSE 0 END) AS positive,
                          SUM(CASE WHEN score < 0 THEN 1 ELSE 0 END) AS negative
                   FROM feedback_state WHERE user_id=? AND target_type=?
                   GROUP BY feedback_type""",
                (user_id, target_type),
            ).fetchall()
        else:
            rows = self.execute(
                """SELECT feedback_type, AVG(score) AS avg_score, COUNT(*) AS count,
                          SUM(CASE WHEN score > 0 THEN 1 ELSE 0 END) AS positive,
                          SUM(CASE WHEN score < 0 THEN 1 ELSE 0 END) AS negative
                   FROM feedback_state WHERE user_id=? GROUP BY feedback_type""",
                (user_id,),
            ).fetchall()
        return {
            str(row["feedback_type"]): {
                "avg_score": float(row["avg_score"] or 0.0),
                "count": int(row["count"] or 0),
                "positive": int(row["positive"] or 0),
                "negative": int(row["negative"] or 0),
            }
            for row in rows
        }

    def create_conversation(
        self,
        user_id: str,
        title: str = "",
        *,
        mode: str = "dialogue",
    ) -> dict[str, Any]:
        self.ensure_user(user_id)
        conversation_id = new_id("conv")
        now = utc_now()
        normalized_mode = normalize_conversation_mode(mode)
        with self.transaction() as conn:
            conn.execute(
                """INSERT INTO conversations(id, user_id, title, last_message, unread_count,
                   is_pinned, is_archived, mode, created_at, updated_at)
                   VALUES(?, ?, ?, '', 0, 0, 0, ?, ?, ?)""",
                (conversation_id, user_id, title[:200], normalized_mode, now, now),
            )
        return self.get_conversation(conversation_id, user_id) or {}

    def get_conversation(self, conversation_id: str, user_id: str) -> dict[str, Any] | None:
        row = self.execute(
            "SELECT * FROM conversations WHERE id=? AND user_id=?",
            (conversation_id, user_id),
        ).fetchone()
        return dict(row) if row else None

    def archive_conversation(self, conversation_id: str, user_id: str) -> bool:
        with self.transaction() as conn:
            cursor = conn.execute(
                "UPDATE conversations SET is_archived=1, updated_at=? WHERE id=? AND user_id=?",
                (utc_now(), conversation_id, user_id),
            )
        return cursor.rowcount > 0

    def set_conversation_archived(
        self, conversation_id: str, user_id: str, archived: bool
    ) -> dict[str, Any] | None:
        """Archive or unarchive a conversation; archived ones drop out of the default list."""
        with self.transaction() as conn:
            conn.execute(
                "UPDATE conversations SET is_archived=?, updated_at=? WHERE id=? AND user_id=?",
                (1 if archived else 0, utc_now(), conversation_id, user_id),
            )
        return self.get_conversation(conversation_id, user_id)

    def delete_conversation(self, conversation_id: str, user_id: str) -> dict[str, Any]:
        """Hard-delete a conversation and its transient history.

        Conversations are chat history, not knowledge provenance (Knowledge Objects
        keep their own Raw Objects), so deletion is immediate rather than two-phase.
        Feedback that targeted this conversation's answers is removed as well, and any
        channel session bound to it is cleared so the next message starts fresh.
        """
        current = self.get_conversation(conversation_id, user_id)
        if not current:
            return {"existed": False, "conversation_id": conversation_id}
        deleted: dict[str, int] = {}
        message_ids_subquery = "SELECT id FROM messages WHERE conversation_id=? AND user_id=?"
        with self.transaction() as conn:
            # feedback_state references feedback(id), so it must be cleared first.
            for table in ("feedback_state", "feedback"):
                cursor = conn.execute(
                    f"DELETE FROM {table} WHERE user_id=? "  # nosec B608
                    f"AND target_id IN ({message_ids_subquery})",
                    (user_id, conversation_id, user_id),
                )
                if cursor.rowcount:
                    deleted[table] = cursor.rowcount
            messages = conn.execute(
                "DELETE FROM messages WHERE conversation_id=? AND user_id=?",
                (conversation_id, user_id),
            )
            deleted["messages"] = messages.rowcount
            sessions = conn.execute(
                "DELETE FROM channel_sessions WHERE user_id=? AND conversation_id=?",
                (user_id, conversation_id),
            )
            if sessions.rowcount:
                deleted["channel_sessions"] = sessions.rowcount
            conversation = conn.execute(
                "DELETE FROM conversations WHERE id=? AND user_id=?",
                (conversation_id, user_id),
            )
            deleted["conversations"] = conversation.rowcount
        return {"existed": True, "conversation_id": conversation_id, "deleted": deleted}

    def set_conversation_mode(self, conversation_id: str, user_id: str, mode: str) -> dict[str, Any] | None:
        normalized_mode = normalize_conversation_mode(mode)
        with self.transaction() as conn:
            cursor = conn.execute(
                "UPDATE conversations SET mode=?, updated_at=? WHERE id=? AND user_id=?",
                (normalized_mode, utc_now(), conversation_id, user_id),
            )
        if cursor.rowcount == 0:
            return None
        return self.get_conversation(conversation_id, user_id)

    def store_message(
        self,
        conversation_id: str,
        user_id: str,
        role: str,
        content: str,
        metadata: dict[str, Any] | None = None,
        reply_to: str | None = None,
    ) -> dict[str, Any]:
        if not self.get_conversation(conversation_id, user_id):
            raise ValueError("Conversation does not belong to user")
        message_id = new_id("msg")
        now = utc_now()
        with self.transaction() as conn:
            conn.execute(
                """INSERT INTO messages(id, conversation_id, user_id, role, content,
                   metadata_json, reply_to, created_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    message_id,
                    conversation_id,
                    user_id,
                    role,
                    content,
                    json.dumps(metadata or {}, ensure_ascii=False, sort_keys=True),
                    reply_to,
                    now,
                ),
            )
            conn.execute(
                "UPDATE conversations SET last_message=?, updated_at=? WHERE id=? AND user_id=?",
                (content[:200], now, conversation_id, user_id),
            )
        row = self.execute(
            "SELECT * FROM messages WHERE id=? AND user_id=?", (message_id, user_id)
        ).fetchone()
        return dict(row) if row else {}

    def get_conversation_messages(
        self,
        conversation_id: str,
        *,
        user_id: str,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        # Select newest N in a subquery, then restore chronological order.
        rows = self.execute(
            """SELECT * FROM (
                   SELECT * FROM messages WHERE conversation_id=? AND user_id=?
                   ORDER BY created_at DESC LIMIT ?
               ) ORDER BY created_at ASC""",
            (conversation_id, user_id, max(1, min(limit, 1000))),
        ).fetchall()
        return [dict(row) for row in rows]

    def get_message(self, message_id: str, user_id: str) -> dict[str, Any] | None:
        row = self.execute(
            "SELECT * FROM messages WHERE id=? AND user_id=?",
            (message_id, user_id),
        ).fetchone()
        return dict(row) if row else None

    def list_conversations(
        self,
        user_id: str,
        *,
        include_archived: bool = False,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        archived_clause = "" if include_archived else " AND is_archived=0"
        # ``archived_clause`` is selected solely by the boolean argument above.
        rows = self.execute(
            f"SELECT * FROM conversations WHERE user_id=?{archived_clause} ORDER BY updated_at DESC LIMIT ?",  # nosec B608
            (user_id, max(1, min(limit, 1000))),
        ).fetchall()
        return [dict(row) for row in rows]

    def get_channel_conversation(self, user_id: str, channel: str, channel_id: str) -> str | None:
        row = self.execute(
            """SELECT s.conversation_id FROM channel_sessions s
               JOIN conversations c ON c.id=s.conversation_id
               WHERE s.user_id=? AND s.channel=? AND s.channel_id=? AND c.is_archived=0""",
            (user_id, channel, channel_id),
        ).fetchone()
        return row["conversation_id"] if row else None

    def get_channel_session(self, user_id: str, channel: str, channel_id: str) -> dict[str, Any] | None:
        row = self.execute(
            """SELECT s.*, c.title, c.is_archived
               FROM channel_sessions s
               JOIN conversations c ON c.id=s.conversation_id AND c.user_id=s.user_id
               WHERE s.user_id=? AND s.channel=? AND s.channel_id=?""",
            (user_id, channel, channel_id),
        ).fetchone()
        return dict(row) if row else None

    def set_channel_conversation(
        self,
        user_id: str,
        channel: str,
        channel_id: str,
        conversation_id: str,
        *,
        mode: str | None = None,
    ) -> None:
        conversation = self.get_conversation(conversation_id, user_id)
        if not conversation:
            raise ValueError("Conversation does not belong to user")
        normalized_mode = normalize_conversation_mode(mode or str(conversation.get("mode") or "dialogue"))
        with self.transaction() as conn:
            conn.execute(
                """INSERT INTO channel_sessions(user_id, channel, channel_id, conversation_id, mode, updated_at)
                   VALUES(?, ?, ?, ?, ?, ?)
                   ON CONFLICT(user_id, channel, channel_id) DO UPDATE SET
                     conversation_id=excluded.conversation_id,
                     mode=excluded.mode,
                     updated_at=excluded.updated_at""",
                (user_id, channel, channel_id, conversation_id, normalized_mode, utc_now()),
            )

    def set_channel_mode(
        self, user_id: str, channel: str, channel_id: str, mode: str
    ) -> dict[str, Any] | None:
        normalized_mode = normalize_conversation_mode(mode)
        session = self.get_channel_session(user_id, channel, channel_id)
        if not session:
            return None
        with self.transaction() as conn:
            conn.execute(
                """UPDATE channel_sessions SET mode=?, updated_at=?
                   WHERE user_id=? AND channel=? AND channel_id=?""",
                (normalized_mode, utc_now(), user_id, channel, channel_id),
            )
            conn.execute(
                "UPDATE conversations SET mode=?, updated_at=? WHERE id=? AND user_id=?",
                (normalized_mode, utc_now(), session["conversation_id"], user_id),
            )
        return self.get_channel_session(user_id, channel, channel_id)

    def clear_channel_conversation(self, user_id: str, channel: str, channel_id: str) -> bool:
        with self.transaction() as conn:
            cursor = conn.execute(
                "DELETE FROM channel_sessions WHERE user_id=? AND channel=? AND channel_id=?",
                (user_id, channel, channel_id),
            )
        return cursor.rowcount > 0

    def log_audit(self, entry: AuditEntry) -> AuditEntry:
        with self.transaction() as conn:
            conn.execute(
                """INSERT INTO audit_log(id, user_id, action, target_type, target_id,
                   before_json, after_json, ip_address, request_id, created_at)
                   VALUES(:id, :user_id, :action, :target_type, :target_id,
                   :before_json, :after_json, :ip_address, :request_id, :created_at)""",
                entry.to_row(),
            )
        return entry

    def list_audit_log(
        self,
        user_id: str | None = None,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        if user_id:
            rows = self.execute(
                "SELECT * FROM audit_log WHERE user_id=? ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (user_id, max(1, min(limit, 5000)), max(0, offset)),
            ).fetchall()
        else:
            rows = self.execute(
                "SELECT * FROM audit_log ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (max(1, min(limit, 5000)), max(0, offset)),
            ).fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------
    # Runtime state, lifecycle, backup, and export
    # ------------------------------------------------------------------

    def idempotency_get(
        self,
        user_id: str,
        request_key: str,
        *,
        request_hash: str = "",
    ) -> dict[str, Any] | None:
        if not request_key:
            return None
        row = self.execute(
            """SELECT request_hash, response_json FROM request_idempotency
               WHERE user_id=? AND request_key=? AND state='complete'""",
            (user_id, request_key),
        ).fetchone()
        if not row:
            return None
        stored_hash = str(row["request_hash"] or "")
        if request_hash and (not stored_hash or not hmac.compare_digest(stored_hash, request_hash)):
            return None
        return _json_load(row["response_json"], {})

    def idempotency_claim(
        self,
        user_id: str,
        request_key: str,
        *,
        request_hash: str = "",
        lease_seconds: int = 300,
    ) -> dict[str, Any]:
        """Atomically claim a request key before any externally visible side effect.

        The durable pending lease closes the check-then-act race both between
        concurrent asyncio tasks and between multiple API worker processes.
        """
        if not request_key:
            return {"status": "disabled", "lease_token": ""}
        user_id = validate_user_id(user_id)
        request_hash = str(request_hash or "").strip().casefold()
        if request_hash and not re.fullmatch(r"[0-9a-f]{64}", request_hash):
            raise ValueError("request_hash must be a lowercase SHA-256 hex digest")
        now = datetime.now(UTC)
        now_text = now.isoformat(timespec="seconds")
        token = new_id("lease")
        with self.transaction() as conn:
            row = conn.execute(
                """SELECT request_hash, response_json, state, lease_token, created_at, updated_at
                   FROM request_idempotency WHERE user_id=? AND request_key=?""",
                (user_id, request_key),
            ).fetchone()
            if row is None:
                conn.execute(
                    """INSERT INTO request_idempotency(
                           user_id, request_key, request_hash, response_json, state,
                           lease_token, created_at, updated_at
                       ) VALUES(?, ?, ?, '{}', 'pending', ?, ?, ?)""",
                    (user_id, request_key, request_hash, token, now_text, now_text),
                )
                return {"status": "acquired", "lease_token": token}

            stored_hash = str(row["request_hash"] or "")
            if request_hash and stored_hash and not hmac.compare_digest(stored_hash, request_hash):
                return {"status": "conflict", "reason": "request_hash_mismatch", "lease_token": ""}

            if row["state"] == "complete":
                # Rows created before schema v7 have no trustworthy payload
                # fingerprint. Replaying them against an arbitrary new body
                # would reintroduce silent data loss, so require a new key.
                if request_hash and not stored_hash:
                    return {"status": "conflict", "reason": "legacy_unbound_key", "lease_token": ""}
                return {
                    "status": "replay",
                    "response": _json_load(row["response_json"], {}),
                    "lease_token": "",
                }

            timestamp = str(row["updated_at"] or row["created_at"] or "")
            try:
                updated_at = datetime.fromisoformat(timestamp)
                if updated_at.tzinfo is None:
                    updated_at = updated_at.replace(tzinfo=UTC)
            except ValueError:
                updated_at = datetime.min.replace(tzinfo=UTC)
            if updated_at <= now - timedelta(seconds=max(1, int(lease_seconds))):
                conn.execute(
                    """UPDATE request_idempotency
                       SET request_hash=?, lease_token=?, response_json='{}', state='pending', updated_at=?
                       WHERE user_id=? AND request_key=?""",
                    (request_hash or stored_hash, token, now_text, user_id, request_key),
                )
                return {"status": "acquired", "lease_token": token, "stale_lease_recovered": True}
            return {"status": "in_progress", "lease_token": ""}

    def idempotency_renew(self, user_id: str, request_key: str, lease_token: str) -> bool:
        if not request_key or not lease_token:
            return False
        with self.transaction() as conn:
            cursor = conn.execute(
                """UPDATE request_idempotency SET updated_at=?
                   WHERE user_id=? AND request_key=? AND state='pending' AND lease_token=?""",
                (utc_now(), user_id, request_key, lease_token),
            )
        return cursor.rowcount == 1

    def idempotency_complete(
        self,
        user_id: str,
        request_key: str,
        lease_token: str,
        response: dict[str, Any],
    ) -> bool:
        if not request_key or not lease_token:
            return False
        with self.transaction() as conn:
            cursor = conn.execute(
                """UPDATE request_idempotency
                   SET response_json=?, state='complete', lease_token='', updated_at=?
                   WHERE user_id=? AND request_key=? AND state='pending' AND lease_token=?""",
                (
                    json.dumps(response, ensure_ascii=False, sort_keys=True),
                    utc_now(),
                    user_id,
                    request_key,
                    lease_token,
                ),
            )
        return cursor.rowcount == 1

    def idempotency_release(self, user_id: str, request_key: str, lease_token: str) -> bool:
        if not request_key or not lease_token:
            return False
        with self.transaction() as conn:
            cursor = conn.execute(
                """DELETE FROM request_idempotency
                   WHERE user_id=? AND request_key=? AND state='pending' AND lease_token=?""",
                (user_id, request_key, lease_token),
            )
        return cursor.rowcount == 1

    def idempotency_store(
        self,
        user_id: str,
        request_key: str,
        response: dict[str, Any],
        *,
        request_hash: str = "",
    ) -> None:
        """Compatibility helper for callers that already completed their work."""
        if not request_key:
            return
        now = utc_now()
        with self.transaction() as conn:
            conn.execute(
                """INSERT INTO request_idempotency(
                       user_id, request_key, request_hash, response_json, state,
                       lease_token, created_at, updated_at
                   ) VALUES(?, ?, ?, ?, 'complete', '', ?, ?)
                   ON CONFLICT(user_id, request_key) DO UPDATE SET
                     request_hash=CASE
                       WHEN request_idempotency.state='complete' THEN request_idempotency.request_hash
                       ELSE excluded.request_hash
                     END,
                     response_json=CASE
                       WHEN request_idempotency.state='complete' THEN request_idempotency.response_json
                       ELSE excluded.response_json
                     END,
                     state=CASE
                       WHEN request_idempotency.state='complete' THEN request_idempotency.state
                       ELSE 'complete'
                     END,
                     lease_token=CASE
                       WHEN request_idempotency.state='complete' THEN request_idempotency.lease_token
                       ELSE ''
                     END,
                     updated_at=excluded.updated_at""",
                (
                    user_id,
                    request_key,
                    request_hash,
                    json.dumps(response, ensure_ascii=False, sort_keys=True),
                    now,
                    now,
                ),
            )

    def idempotency_prune(self, *, days: int = 30) -> int:
        with self.transaction() as conn:
            cursor = conn.execute(
                """DELETE FROM request_idempotency
                   WHERE datetime(COALESCE(NULLIF(updated_at, ''), created_at)) < datetime('now', ?)""",
                (f"-{max(1, min(int(days), 365))} days",),
            )
        return cursor.rowcount

    def kv_get(self, key: str) -> str | None:
        row = self.execute("SELECT value FROM runtime_kv WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def kv_set(self, key: str, value: str) -> None:
        with self.transaction() as conn:
            conn.execute(
                """INSERT INTO runtime_kv(key, value, updated_at) VALUES(?, ?, ?)
                   ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at""",
                (key, value, utc_now()),
            )

    def kv_delete(self, key: str) -> None:
        with self.transaction() as conn:
            conn.execute("DELETE FROM runtime_kv WHERE key=?", (key,))

    def kv_list_prefix(self, prefix: str) -> list[dict[str, Any]]:
        escaped = prefix.replace("%", r"\%").replace("_", r"\_")
        rows = self.execute(
            "SELECT * FROM runtime_kv WHERE key LIKE ? ESCAPE '\\' ORDER BY key",
            (f"{escaped}%",),
        ).fetchall()
        return [dict(row) for row in rows]

    _BRIDGE_NONCE_PREFIX = "bridge_nonce:"

    def claim_bridge_nonce(self, nonce: str) -> bool:
        """Atomically record a single-use bridge nonce; return False on replay.

        The claim is a race-free INSERT-if-absent under BEGIN IMMEDIATE, so two
        concurrent requests carrying the same nonce cannot both be accepted.
        """
        if not nonce:
            return False
        with self.transaction() as conn:
            cursor = conn.execute(
                """INSERT INTO runtime_kv(key, value, updated_at) VALUES(?, '1', ?)
                   ON CONFLICT(key) DO NOTHING""",
                (f"{self._BRIDGE_NONCE_PREFIX}{nonce}", utc_now()),
            )
        return cursor.rowcount == 1

    def prune_bridge_nonces(self, *, max_age_sec: int) -> int:
        """Drop bridge nonces older than the replay window; return rows removed."""
        cutoff = max(1, int(max_age_sec))
        with self.transaction() as conn:
            cursor = conn.execute(
                "DELETE FROM runtime_kv WHERE key LIKE ? ESCAPE '\\' "  # nosec B608
                "AND datetime(updated_at) < datetime('now', ?)",
                (f"{self._BRIDGE_NONCE_PREFIX}%", f"-{cutoff} seconds"),
            )
        return cursor.rowcount

    def list_knowledge_missing_embedding(self, model: str, *, limit: int = 64) -> list[dict[str, Any]]:
        """Knowledge Objects whose stored vector is absent, from another model, or stale.

        Staleness is keyed on the Knowledge Object ``version``, which bumps on every
        content-affecting update, so a re-enriched note is re-embedded on the next
        index cycle while a lifecycle-only change is not.
        """
        bounded = max(1, min(int(limit), 1000))
        rows = self.execute(
            """SELECT k.id AS id, k.user_id AS user_id, k.version AS version,
                      k.title AS title, k.summary AS summary, k.content AS content,
                      k.tags_json AS tags_json, k.knowledge_kind AS knowledge_kind
               FROM knowledge_objects k
               LEFT JOIN knowledge_embeddings e ON e.knowledge_object_id = k.id
               WHERE k.deleted_at IS NULL
                 AND (e.knowledge_object_id IS NULL
                      OR e.model != ?
                      OR e.source_version != k.version)
               ORDER BY k.updated_at DESC
               LIMIT ?""",
            (model, bounded),
        ).fetchall()
        return [dict(row) for row in rows]

    def get_user_embeddings(
        self, user_id: str, model: str, dim: int, *, limit: int | None = None
    ) -> list[tuple[str, bytes]]:
        """Return (knowledge_object_id, packed vector) for a user's live vectors.

        Only rows matching the active model and dimension are returned, and vectors
        whose Knowledge Object has been soft-deleted are excluded, so a stale row can
        never resurrect deleted knowledge into dense recall. ``limit`` caps the scan
        to the newest N objects (a latency guard on a large corpus).
        """
        query = (
            "SELECT e.knowledge_object_id AS id, e.vector AS vector "
            "FROM knowledge_embeddings e "
            "JOIN knowledge_objects k ON k.id = e.knowledge_object_id "
            "WHERE e.user_id = ? AND e.model = ? AND e.dim = ? AND k.deleted_at IS NULL"
        )
        params: list[Any] = [user_id, model, int(dim)]
        if limit is not None and limit > 0:
            query += " ORDER BY k.created_at DESC LIMIT ?"
            params.append(int(limit))
        rows = self.execute(query, tuple(params)).fetchall()
        return [(str(row["id"]), bytes(row["vector"])) for row in rows]

    def get_user_vectors(self, user_id: str, model: str, *, limit: int = 5000) -> list[tuple[str, bytes]]:
        """All live (ko_id, packed vector) for a model, any dimension.

        Used by near-duplicate detection, which compares stored vectors against
        each other rather than against a query, so it does not know the dim up
        front. Soft-deleted objects are excluded. Ordered newest-first so a hard
        cap keeps the most recent corpus.
        """
        rows = self.execute(
            """SELECT e.knowledge_object_id AS id, e.vector AS vector
               FROM knowledge_embeddings e
               JOIN knowledge_objects k ON k.id = e.knowledge_object_id
               WHERE e.user_id = ? AND e.model = ? AND k.deleted_at IS NULL
               ORDER BY k.created_at DESC LIMIT ?""",
            (user_id, model, max(1, int(limit))),
        ).fetchall()
        return [(str(row["id"]), bytes(row["vector"])) for row in rows]

    def upsert_knowledge_embeddings(self, items: Sequence[dict[str, Any]]) -> int:
        """Insert or replace a batch of vectors in one transaction; return the count."""
        if not items:
            return 0
        now = utc_now()
        with self.transaction() as conn:
            for item in items:
                conn.execute(
                    """INSERT INTO knowledge_embeddings(
                           knowledge_object_id, user_id, model, dim,
                           source_version, content_hash, vector, updated_at)
                       VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(knowledge_object_id) DO UPDATE SET
                           user_id=excluded.user_id,
                           model=excluded.model,
                           dim=excluded.dim,
                           source_version=excluded.source_version,
                           content_hash=excluded.content_hash,
                           vector=excluded.vector,
                           updated_at=excluded.updated_at""",
                    (
                        str(item["knowledge_object_id"]),
                        str(item["user_id"]),
                        str(item["model"]),
                        int(item["dim"]),
                        int(item.get("source_version", 0)),
                        str(item.get("content_hash", "")),
                        bytes(item["vector"]),
                        now,
                    ),
                )
        return len(items)

    def delete_knowledge_embedding(self, knowledge_object_id: str) -> None:
        with self.transaction() as conn:
            conn.execute(
                "DELETE FROM knowledge_embeddings WHERE knowledge_object_id=?",
                (knowledge_object_id,),
            )

    def count_knowledge_embeddings(self, user_id: str | None = None) -> int:
        if user_id is None:
            row = self.execute("SELECT COUNT(*) AS n FROM knowledge_embeddings").fetchone()
        else:
            row = self.execute(
                "SELECT COUNT(*) AS n FROM knowledge_embeddings WHERE user_id=?",
                (user_id,),
            ).fetchone()
        return int(row["n"]) if row else 0

    def list_lifecycle_candidates(
        self,
        user_id: str,
        *,
        days_threshold: int = 90,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        """Return explainable, review-only stale-knowledge candidates.

        This intentionally does not mutate lifecycle or importance. Manually
        reviewed, file-derived, recently used, or positively rated knowledge is
        protected from automated suggestions.
        """

        days = max(1, min(int(days_threshold), 36500))
        rows = self.execute(
            """SELECT k.*, u.retrieval_count, u.answer_count,
                      u.positive_feedback_count, u.negative_feedback_count,
                      u.last_retrieved_at, u.last_used_at, u.last_feedback_at
               FROM knowledge_objects k
               LEFT JOIN knowledge_usage u
                 ON u.user_id=k.user_id AND u.knowledge_object_id=k.id
               WHERE k.user_id=? AND k.lifecycle_stage='active' AND k.deleted_at IS NULL
                 AND datetime(k.updated_at) < datetime('now', ?)
               ORDER BY k.importance ASC, k.updated_at ASC LIMIT ?""",
            (user_id, f"-{days} days", max(1, min(int(limit), 5000))),
        ).fetchall()
        output: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            metadata = _json_load(item.get("metadata_json"), {})
            assessment = metadata.get("promotion_assessment") if isinstance(metadata, dict) else {}
            assessment = assessment if isinstance(assessment, dict) else {}
            protected_reasons: list[str] = []
            if item.get("content_type") == "file" or metadata.get("source_filename"):
                protected_reasons.append("file-derived knowledge")
            if assessment.get("reason") in {"explicit save intent", "human review"}:
                protected_reasons.append("explicitly saved or reviewed")
            if int(item.get("positive_feedback_count") or 0) > int(item.get("negative_feedback_count") or 0):
                protected_reasons.append("positive user feedback")
            recent_cutoff = datetime.now(UTC) - timedelta(days=max(7, days // 3))
            for field, label in (
                ("last_used_at", "recently used in an answer"),
                ("last_retrieved_at", "recently retrieved"),
            ):
                timestamp = str(item.get(field) or "")
                if not timestamp:
                    continue
                try:
                    parsed = datetime.fromisoformat(timestamp)
                    if parsed.tzinfo is None:
                        parsed = parsed.replace(tzinfo=UTC)
                    if parsed >= recent_cutoff:
                        protected_reasons.append(label)
                except ValueError:
                    pass
            if protected_reasons:
                continue

            importance = max(0.0, min(1.0, float(item.get("importance") or 0.5)))
            quality = max(0.0, min(1.0, float(item.get("quality_score") or 0.5)))
            promotion = max(0.0, min(1.0, float(item.get("promotion_score") or 0.5)))
            retrievals = int(item.get("retrieval_count") or 0)
            answers = int(item.get("answer_count") or 0)
            negative = int(item.get("negative_feedback_count") or 0)
            risk = (
                (1.0 - importance) * 0.38
                + (1.0 - quality) * 0.22
                + (1.0 - promotion) * 0.16
                + (0.12 if retrievals == 0 else 0.0)
                + (0.08 if answers == 0 else 0.0)
                + min(0.12, negative * 0.04)
            )
            if risk < 0.48:
                continue
            reasons = ["not updated within threshold"]
            if importance < 0.35:
                reasons.append("low importance")
            if quality < 0.4:
                reasons.append("low quality score")
            if promotion < 0.4:
                reasons.append("weak original promotion confidence")
            if retrievals == 0 and answers == 0:
                reasons.append("never used")
            if negative:
                reasons.append("negative feedback")
            output.append(
                {
                    "knowledge_object": item,
                    "risk_score": round(min(1.0, risk), 4),
                    "recommended_action": "review_for_archive" if risk >= 0.68 else "review_importance",
                    "suggested_importance": round(max(0.1, importance - min(0.2, risk * 0.15)), 3),
                    "reasons": reasons,
                    "protected": False,
                }
            )
        return sorted(output, key=lambda item: item["risk_score"], reverse=True)

    def deprecate_stale_knowledge(self, user_id: str, days_threshold: int = 90) -> int:
        """Archive low-value stale items, creating a version for every transition."""
        days = max(1, min(int(days_threshold), 36500))
        rows = self.execute(
            """SELECT id FROM knowledge_objects
               WHERE user_id=? AND lifecycle_stage='active' AND deleted_at IS NULL
                 AND importance < 0.3
                 AND datetime(updated_at) < datetime('now', ?)""",
            (user_id, f"-{days} days"),
        ).fetchall()
        changed = 0
        for row in rows:
            if self.update_knowledge_fields(
                str(row["id"]),
                user_id,
                lifecycle_stage=LifecycleStage.ARCHIVED.value,
            ):
                changed += 1
        return changed

    def get_lifecycle_stats(self, user_id: str) -> dict[str, int]:
        rows = self.execute(
            """SELECT lifecycle_stage, COUNT(*) AS count FROM knowledge_objects
               WHERE user_id=? GROUP BY lifecycle_stage""",
            (user_id,),
        ).fetchall()
        result = {stage.value: 0 for stage in LifecycleStage}
        result.update({row["lifecycle_stage"]: int(row["count"]) for row in rows})
        return result

    def _verify_backup_conn(self, backup_conn: sqlite3.Connection) -> tuple[str, list[Any], int]:
        """Integrity / foreign-key / schema check of a backup copy.

        Runs entirely against the backup's own connection, independent of the
        live per-thread connections, so a backup never freezes concurrent
        requests during the full-DB scan.
        """
        integrity = backup_conn.execute("PRAGMA integrity_check").fetchone()[0]
        foreign_key_violations = backup_conn.execute("PRAGMA foreign_key_check").fetchall()
        schema_row = backup_conn.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone()
        backup_schema_version = int(schema_row[0]) if schema_row else -1
        return integrity, foreign_key_violations, backup_schema_version

    def create_backup(self, *, label: str = "manual") -> dict[str, Any]:
        self.settings.backups_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        stem = f"jericho-{timestamp}-{_safe_filename(label)}"
        destination = self.settings.backups_dir / f"{stem}.sqlite3"
        suffix = 1
        while destination.exists():
            destination = self.settings.backups_dir / f"{stem}-{suffix}.sqlite3"
            suffix += 1

        backup_conn = sqlite3.connect(str(destination))
        try:
            # Checkpoint + copy run on this thread's own connection. SQLite's online
            # backup API takes a transactionally consistent snapshot and restarts if
            # a concurrent writer modifies the source, so no cross-thread lock is
            # needed; a PASSIVE checkpoint never blocks other connections. The
            # integrity/foreign-key/schema scan then runs on the backup copy.
            self.conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
            self.conn.backup(backup_conn)
            integrity, foreign_key_violations, backup_schema_version = self._verify_backup_conn(backup_conn)
        except BaseException:
            destination.unlink(missing_ok=True)
            raise
        finally:
            backup_conn.close()
        if integrity != "ok":
            destination.unlink(missing_ok=True)
            raise RuntimeError(f"Backup integrity check failed: {integrity}")
        if foreign_key_violations:
            destination.unlink(missing_ok=True)
            raise RuntimeError(f"Backup foreign-key check failed: {len(foreign_key_violations)} violation(s)")
        if backup_schema_version != SCHEMA_VERSION:
            destination.unlink(missing_ok=True)
            raise RuntimeError(
                f"Backup schema mismatch: database={backup_schema_version}, expected={SCHEMA_VERSION}"
            )

        _chmod_private(destination)
        digest = _sha256_file(destination)
        manifest = {
            "schema_version": backup_schema_version,
            "created_at": utc_now(),
            "label": label,
            "database": destination.name,
            "size_bytes": destination.stat().st_size,
            "sha256": digest,
            "integrity_check": integrity,
            "foreign_key_violations": 0,
            # A database backup is transactionally consistent, but binary raw
            # files and the Markdown vault deliberately remain separate so an
            # operator cannot mistake this for a full installation backup.
            "scope": {
                "sqlite_database": "included",
                "raw_files": "external",
                "memory_vault": "external",
                "model_weights": "external",
                "configuration_and_secrets": "external",
            },
        }
        manifest_path = destination.with_suffix(".manifest.json")
        _write_json_atomic(manifest_path, manifest)
        return {**manifest, "path": str(destination), "manifest_path": str(manifest_path)}

    def list_backups(self) -> list[dict[str, Any]]:
        self.settings.backups_dir.mkdir(parents=True, exist_ok=True)
        results: list[dict[str, Any]] = []
        for manifest_path in sorted(self.settings.backups_dir.glob("*.manifest.json"), reverse=True):
            try:
                if manifest_path.is_symlink():
                    continue
                data = json.loads(manifest_path.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    continue
                database_name = str(data["database"])
                if Path(database_name).name != database_name or not database_name.endswith(".sqlite3"):
                    continue
                database_candidate = self.settings.backups_dir / database_name
                if database_candidate.is_symlink():
                    continue
                database_path = database_candidate.resolve()
                if database_path.parent != self.settings.backups_dir.resolve():
                    continue
                data["path"] = str(database_path)
                data["manifest_path"] = str(manifest_path)
                results.append(data)
            except (OSError, json.JSONDecodeError, KeyError, TypeError):
                continue
        return results

    def verify_backup(self, filename: str) -> dict[str, Any]:
        requested_name = str(filename or "")
        safe_name = Path(requested_name).name
        if (
            not requested_name
            or requested_name != safe_name
            or "/" in requested_name
            or "\\" in requested_name
        ):
            raise FileNotFoundError("Backup filename must not contain path components")
        candidate = self.settings.backups_dir / safe_name
        if candidate.is_symlink():
            raise FileNotFoundError("Backup symlinks are not allowed")
        path = candidate.resolve()
        if (
            path.parent != self.settings.backups_dir.resolve()
            or path.suffix.casefold() != ".sqlite3"
            or not path.is_file()
        ):
            raise FileNotFoundError("Backup not found")
        integrity = "error"
        database_error: str | None = None
        database_schema_version: int | None = None
        database_schema_supported = False
        foreign_key_violations: int | None = None
        conn = sqlite3.connect(str(path))
        try:
            conn.execute("PRAGMA query_only=ON")
            integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
            foreign_key_violations = len(conn.execute("PRAGMA foreign_key_check").fetchall())
            schema_row = conn.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()
            if schema_row is None:
                database_error = "Database does not contain a schema_version marker"
            else:
                try:
                    database_schema_version = int(schema_row[0])
                    database_schema_supported = 0 <= database_schema_version <= SCHEMA_VERSION
                except (TypeError, ValueError):
                    database_error = "Database schema_version marker is invalid"
        except sqlite3.DatabaseError as exc:
            database_error = f"{type(exc).__name__}: {exc}"
        finally:
            conn.close()
        actual_size = path.stat().st_size
        digest = _sha256_file(path)
        manifest_path = path.with_suffix(".manifest.json")
        expected_digest: str | None = None
        manifest_error: str | None = None
        manifest_database_matches: bool | None = None
        manifest_size_matches: bool | None = None
        manifest_schema_supported: bool | None = None
        manifest_schema_matches_database: bool | None = None
        manifest_present = manifest_path.is_file() and not manifest_path.is_symlink()
        if manifest_path.is_symlink():
            manifest_error = "Manifest symlinks are not allowed"
        elif manifest_present:
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                if not isinstance(manifest, dict):
                    raise ValueError("Manifest root must be an object")
                expected_digest = str(manifest.get("sha256") or "") or None
                if expected_digest is None:
                    manifest_error = "Manifest does not contain sha256"
                manifest_database_matches = str(manifest.get("database") or "") == path.name
                manifest_size_value = manifest.get("size_bytes")
                try:
                    if manifest_size_value is None or isinstance(manifest_size_value, bool):
                        raise TypeError
                    manifest_size_matches = int(manifest_size_value) == actual_size
                except (TypeError, ValueError):
                    manifest_size_matches = False
                manifest_schema_value = manifest.get("schema_version")
                try:
                    if manifest_schema_value is None or isinstance(manifest_schema_value, bool):
                        raise TypeError
                    manifest_schema = int(manifest_schema_value)
                    manifest_schema_supported = 0 <= manifest_schema <= SCHEMA_VERSION
                    manifest_schema_matches_database = (
                        database_schema_version is not None and manifest_schema == database_schema_version
                    )
                except (TypeError, ValueError):
                    manifest_schema_supported = False
                    manifest_schema_matches_database = False
                validation_errors: list[str] = []
                if not manifest_database_matches:
                    validation_errors.append("database filename does not match")
                if not manifest_size_matches:
                    validation_errors.append("size_bytes does not match")
                if not manifest_schema_supported:
                    validation_errors.append("schema_version is unsupported")
                elif not manifest_schema_matches_database:
                    validation_errors.append("schema_version does not match the database")
                if validation_errors:
                    manifest_error = "; ".join(validation_errors)
            except (OSError, json.JSONDecodeError, ValueError) as exc:
                manifest_error = f"{type(exc).__name__}: {exc}"
        else:
            manifest_error = "Manifest is missing"
        hash_matches_manifest = bool(expected_digest and hmac.compare_digest(digest, expected_digest))
        return {
            "database": path.name,
            "size_bytes": actual_size,
            "sha256": digest,
            "integrity_check": integrity,
            "database_schema_version": database_schema_version,
            "database_schema_supported": database_schema_supported,
            "foreign_key_violations": foreign_key_violations,
            "database_error": database_error,
            "manifest_present": manifest_present,
            "hash_matches_manifest": hash_matches_manifest if manifest_present else None,
            "manifest_database_matches": manifest_database_matches,
            "manifest_size_matches": manifest_size_matches,
            "manifest_schema_supported": manifest_schema_supported,
            "manifest_schema_matches_database": manifest_schema_matches_database,
            "manifest_error": manifest_error,
            "ok": (
                integrity == "ok"
                and database_error is None
                and database_schema_supported
                and foreign_key_violations == 0
                and manifest_present
                and manifest_error is None
                and hash_matches_manifest
                and manifest_database_matches is True
                and manifest_size_matches is True
                and manifest_schema_supported is True
                and manifest_schema_matches_database is True
            ),
        }

    def restore_backup(self, filename: str, *, safety_label: str = "pre-restore") -> dict[str, Any]:
        """Atomically restore a verified SQLite backup while Jericho is stopped.

        The current process must own the exclusive backend lease.  The exact
        active DB/WAL/SHM bytes are staged first for automatic rollback.  A
        verified online safety backup is created when the active database can be
        opened; otherwise an explicitly unverified recovery bundle preserves the
        original files instead of making a destructive restore unavoidable.

        Only the SQLite knowledge database is restored.  Content-addressed
        files, Markdown vault, Telegram queue, model weights and secrets remain
        external, matching the backup manifest.
        """

        from jericho.diagnostics.runtime_lease import process_owns_lease

        lease_path = self.settings.state_dir / "backend.lock"
        if not process_owns_lease(lease_path, protocol="jericho.backend.v1"):
            raise RuntimeError(
                "Database restore requires the exclusive backend process lease; "
                "stop Jericho and use `jericho restore-backup ... --yes`"
            )

        verification = self.verify_backup(filename)
        if not verification.get("ok"):
            problems = [
                str(verification.get(key))
                for key in ("database_error", "manifest_error", "integrity_check")
                if verification.get(key) not in (None, "", "ok")
            ]
            detail = "; ".join(problems) or "backup verification failed"
            raise RuntimeError(f"Refusing to restore unverified backup: {detail}")

        backup_name = str(verification["database"])
        backup_path = (self.settings.backups_dir / backup_name).resolve()
        database_path = self._db_path.absolute()
        active_paths = [database_path, Path(f"{database_path}-wal"), Path(f"{database_path}-shm")]
        if any(path.is_symlink() for path in active_paths):
            raise RuntimeError("Database path and SQLite sidecars must not be symlinks during restore")

        # Stop using the active database before taking exact rollback copies.
        self.close()
        rollback_snapshots: dict[Path, Path] = {}
        staged: Path | None = None
        safety_backup: dict[str, Any] | None = None
        recovery_snapshot: dict[str, Any] | None = None
        try:
            for active_path in active_paths:
                if active_path.is_file():
                    rollback_snapshots[active_path] = _stage_private_copy(active_path, active_path)

            if database_path.is_file():
                try:
                    safety_backup = self.create_backup(label=safety_label)
                except BaseException as exc:
                    self.close()
                    if not rollback_snapshots:
                        raise RuntimeError("Could not preserve the active database before restore") from exc
                    recovery_snapshot = _write_recovery_bundle(
                        self.settings,
                        rollback_snapshots,
                        label=safety_label,
                        reason_type=type(exc).__name__,
                    )
                finally:
                    self.close()

            staged = _stage_private_copy(backup_path, database_path)
            staged_size = staged.stat().st_size
            staged_digest = _sha256_file(staged)
            if staged_size != int(verification["size_bytes"]) or not hmac.compare_digest(
                staged_digest,
                str(verification["sha256"]),
            ):
                raise RuntimeError("Backup changed while it was staged for restore")

            for active_path in active_paths[1:]:
                active_path.unlink(missing_ok=True)
            os.replace(staged, database_path)
            _chmod_private(database_path)
            _fsync_directory(database_path.parent)

            # Opening performs only supported forward migrations.  Health must
            # pass before rollback snapshots are discarded.
            health = self.diagnostics()
            if not health.get("ok"):
                raise RuntimeError(
                    "Restored database failed health checks: "
                    f"integrity={health.get('integrity_check')}, "
                    f"foreign_keys={len(health.get('foreign_key_violations') or [])}"
                )
            return {
                "ok": True,
                "restored_from": backup_name,
                "restored_sha256": verification["sha256"],
                "source_schema_version": verification["database_schema_version"],
                "active_schema_version": health["schema_version"],
                "database_path": str(database_path),
                "safety_backup": safety_backup,
                "recovery_snapshot": recovery_snapshot,
                "scope": {
                    "sqlite_database": "restored",
                    "raw_files": "unchanged",
                    "memory_vault": "unchanged",
                    "telegram_queue": "unchanged",
                    "model_weights": "unchanged",
                    "configuration_and_secrets": "unchanged",
                },
                "integrity_check": health["integrity_check"],
                "foreign_key_violations": len(health.get("foreign_key_violations") or []),
            }
        except BaseException as restore_error:
            self.close()
            rollback_error: BaseException | None = None
            try:
                for active_path in active_paths:
                    active_path.unlink(missing_ok=True)
                for original, snapshot in rollback_snapshots.items():
                    os.replace(snapshot, original)
                    _chmod_private(original)
                _fsync_directory(database_path.parent)
            except BaseException as exc:
                rollback_error = exc
            if rollback_error is not None:
                raise RuntimeError(
                    "Database restore failed and exact-file rollback also failed: "
                    f"restore={type(restore_error).__name__}: {restore_error}; "
                    f"rollback={type(rollback_error).__name__}: {rollback_error}"
                ) from restore_error
            recovery = (
                "the exact previous database files were restored automatically"
                if rollback_snapshots
                else "the failed replacement was removed; no previous database existed"
            )
            raise RuntimeError(
                f"Database restore failed; {recovery}: {type(restore_error).__name__}: {restore_error}"
            ) from restore_error
        finally:
            if staged is not None:
                staged.unlink(missing_ok=True)
            for snapshot in rollback_snapshots.values():
                snapshot.unlink(missing_ok=True)

    def export_user(self, user_id: str) -> dict[str, Any]:
        user = self.get_user(user_id)
        if not user:
            raise ValueError("User not found")
        table_queries = {
            "raw_objects": ("SELECT * FROM raw_objects WHERE user_id=?", (user_id,)),
            "knowledge_objects": ("SELECT * FROM knowledge_objects WHERE user_id=?", (user_id,)),
            "inbox": ("SELECT * FROM inbox WHERE user_id=?", (user_id,)),
            "entities": ("SELECT * FROM entities WHERE user_id=?", (user_id,)),
            "knowledge_entity_links": ("SELECT * FROM knowledge_entity_links WHERE user_id=?", (user_id,)),
            "relations": ("SELECT * FROM relations WHERE user_id=?", (user_id,)),
            "entity_resolution_candidates": (
                "SELECT * FROM entity_resolution_candidates WHERE user_id=?",
                (user_id,),
            ),
            "knowledge_object_versions": (
                "SELECT * FROM knowledge_object_versions WHERE user_id=?",
                (user_id,),
            ),
            "entity_versions": ("SELECT * FROM entity_versions WHERE user_id=?", (user_id,)),
            "entity_merge_history": (
                "SELECT * FROM entity_merge_history WHERE user_id=?",
                (user_id,),
            ),
            "user_permission_overrides": (
                "SELECT * FROM user_permission_overrides WHERE user_id=?",
                (user_id,),
            ),
            "feedback": ("SELECT * FROM feedback WHERE user_id=?", (user_id,)),
            "feedback_state": ("SELECT * FROM feedback_state WHERE user_id=?", (user_id,)),
            "knowledge_usage": ("SELECT * FROM knowledge_usage WHERE user_id=?", (user_id,)),
            "relation_candidates": ("SELECT * FROM relation_candidates WHERE user_id=?", (user_id,)),
            "knowledge_conflicts": ("SELECT * FROM knowledge_conflicts WHERE user_id=?", (user_id,)),
            "conversations": ("SELECT * FROM conversations WHERE user_id=?", (user_id,)),
            "messages": ("SELECT * FROM messages WHERE user_id=?", (user_id,)),
            "channel_sessions": ("SELECT * FROM channel_sessions WHERE user_id=?", (user_id,)),
            "request_idempotency": (
                """SELECT user_id, request_key, request_hash, response_json, state, created_at, updated_at
                   FROM request_idempotency WHERE user_id=? AND state='complete'""",
                (user_id,),
            ),
            "audit_log": ("SELECT * FROM audit_log WHERE user_id=?", (user_id,)),
        }
        payload: dict[str, Any] = {
            "format": "jericho-user-export-v3",
            "exported_at": utc_now(),
            "user": user,
        }
        for key, (query, params) in table_queries.items():
            payload[key] = [dict(row) for row in self.execute(query, params).fetchall()]
        self.settings.exports_dir.mkdir(parents=True, exist_ok=True)
        identity_hash = hashlib.sha256(user_id.encode("utf-8", errors="replace")).hexdigest()[:12]
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
        path = self.settings.exports_dir / (
            f"jericho-export-{_safe_filename(user_id)}--{identity_hash}-{timestamp}.json"
        )
        _write_json_atomic(path, payload)
        return {"path": str(path), "filename": path.name, "size_bytes": path.stat().st_size}

    def diagnostics(self) -> dict[str, Any]:
        integrity = self.execute("PRAGMA integrity_check").fetchone()[0]
        foreign_keys = [dict(row) for row in self.execute("PRAGMA foreign_key_check").fetchall()]
        counts: dict[str, int] = {}
        for table in (
            "users",
            "raw_objects",
            "knowledge_objects",
            "inbox",
            "entities",
            "relations",
            "relation_candidates",
            "knowledge_conflicts",
            "feedback",
            "feedback_state",
            "knowledge_usage",
            "conversations",
            "messages",
            "channel_sessions",
        ):
            # ``table`` comes only from the fixed diagnostic allowlist above.
            row = self.execute(f"SELECT COUNT(*) AS count FROM {table}").fetchone()  # nosec B608
            counts[table] = int(row["count"] if row else 0)
        return {
            "database_path": str(self._db_path),
            "database_size_bytes": self._db_path.stat().st_size if self._db_path.exists() else 0,
            "schema_version": SCHEMA_VERSION,
            "integrity_check": integrity,
            "foreign_key_violations": foreign_keys,
            "fts_available": self._fts_available,
            "counts": counts,
            "ok": integrity == "ok" and not foreign_keys,
        }

    # ---- Missions / Tasks ----

    _MISSION_UPDATABLE = frozenset(
        {
            "goal",
            "title",
            "status",
            "plan_summary",
            "created_by",
            "error",
            "task_count",
            "done_count",
            "started_at",
            "completed_at",
        }
    )
    _MISSION_TASK_UPDATABLE = frozenset(
        {
            "status",
            "result",
            "inbox_id",
            "tools_used_json",
            "error",
            "started_at",
            "completed_at",
        }
    )

    def create_mission(self, mission: Mission) -> dict[str, Any]:
        """Persist a mission header; tasks are added separately via set_mission_plan."""
        self.ensure_user(mission.user_id)
        with self.transaction() as conn:
            conn.execute(
                """INSERT INTO missions(id, user_id, goal, title, status, origin, plan_summary,
                   created_by, error, task_count, done_count, metadata_json, version,
                   created_at, updated_at, started_at, completed_at)
                   VALUES(:id, :user_id, :goal, :title, :status, :origin, :plan_summary,
                   :created_by, :error, :task_count, :done_count, :metadata_json, :version,
                   :created_at, :updated_at, :started_at, :completed_at)""",
                mission.to_row(),
            )
        return self.get_mission(mission.id, mission.user_id) or {}

    def set_mission_plan(
        self,
        mission_id: str,
        user_id: str,
        tasks: list[MissionTask],
        *,
        plan_summary: str,
        status: MissionStatus | str,
    ) -> dict[str, Any] | None:
        """Attach a task plan to a mission atomically and record its size."""
        now = utc_now()
        with self.transaction() as conn:
            owned = conn.execute(
                "SELECT id FROM missions WHERE id=? AND user_id=?",
                (mission_id, user_id),
            ).fetchone()
            if owned is None:
                return None
            conn.execute(
                "DELETE FROM mission_tasks WHERE mission_id=? AND user_id=?",
                (mission_id, user_id),
            )
            for task in tasks:
                conn.execute(
                    """INSERT INTO mission_tasks(id, mission_id, user_id, seq, kind, title,
                       instruction, depends_on_json, status, result, inbox_id, tools_used_json,
                       error, created_at, updated_at, started_at, completed_at)
                       VALUES(:id, :mission_id, :user_id, :seq, :kind, :title, :instruction,
                       :depends_on_json, :status, :result, :inbox_id, :tools_used_json, :error,
                       :created_at, :updated_at, :started_at, :completed_at)""",
                    task.to_row(),
                )
            conn.execute(
                """UPDATE missions SET plan_summary=?, status=?, task_count=?, done_count=0,
                   updated_at=? WHERE id=? AND user_id=?""",
                (plan_summary, enum_value(status), len(tasks), now, mission_id, user_id),
            )
        return self.get_mission(mission_id, user_id)

    def get_mission(self, mission_id: str, user_id: str | None = None) -> dict[str, Any] | None:
        if user_id is None:
            row = self.execute("SELECT * FROM missions WHERE id=?", (mission_id,)).fetchone()
        else:
            row = self.execute(
                "SELECT * FROM missions WHERE id=? AND user_id=?",
                (mission_id, user_id),
            ).fetchone()
        return dict(row) if row else None

    def list_missions(
        self,
        user_id: str | None = None,
        *,
        status: MissionStatus | str | None = None,
        statuses: Sequence[str] | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if user_id is not None:
            clauses.append("user_id=?")
            params.append(user_id)
        if status is not None:
            clauses.append("status=?")
            params.append(enum_value(status))
        elif statuses:
            placeholders = ",".join("?" for _ in statuses)
            clauses.append(f"status IN ({placeholders})")
            params.extend(statuses)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.extend([max(1, min(limit, 5000)), max(0, offset)])
        rows = self.execute(
            f"SELECT * FROM missions {where} ORDER BY created_at DESC LIMIT ? OFFSET ?",  # nosec B608
            tuple(params),
        ).fetchall()
        return [dict(row) for row in rows]

    def count_missions(self, user_id: str, *, statuses: Sequence[str] | None = None) -> int:
        params: list[Any] = [user_id]
        clause = "user_id=?"
        if statuses:
            placeholders = ",".join("?" for _ in statuses)
            clause += f" AND status IN ({placeholders})"
            params.extend(statuses)
        row = self.execute(
            f"SELECT COUNT(*) AS n FROM missions WHERE {clause}",  # nosec B608
            tuple(params),
        ).fetchone()
        return int(row["n"]) if row else 0

    def get_mission_tasks(self, mission_id: str, user_id: str | None = None) -> list[dict[str, Any]]:
        if user_id is None:
            rows = self.execute(
                "SELECT * FROM mission_tasks WHERE mission_id=? ORDER BY seq",
                (mission_id,),
            ).fetchall()
        else:
            rows = self.execute(
                "SELECT * FROM mission_tasks WHERE mission_id=? AND user_id=? ORDER BY seq",
                (mission_id, user_id),
            ).fetchall()
        return [dict(row) for row in rows]

    def update_mission_fields(self, mission_id: str, user_id: str, **fields: Any) -> bool:
        updates = {key: value for key, value in fields.items() if key in self._MISSION_UPDATABLE}
        if not updates:
            return False
        assignments = ", ".join(f"{column}=?" for column in updates)
        params = [*updates.values(), utc_now(), mission_id, user_id]
        with self.transaction() as conn:
            cursor = conn.execute(
                f"UPDATE missions SET {assignments}, version=version+1, updated_at=? "  # nosec B608
                "WHERE id=? AND user_id=?",
                tuple(params),
            )
        return cursor.rowcount > 0

    def update_mission_task_fields(self, task_id: str, user_id: str, **fields: Any) -> bool:
        updates = {key: value for key, value in fields.items() if key in self._MISSION_TASK_UPDATABLE}
        if not updates:
            return False
        assignments = ", ".join(f"{column}=?" for column in updates)
        params = [*updates.values(), utc_now(), task_id, user_id]
        with self.transaction() as conn:
            cursor = conn.execute(
                f"UPDATE mission_tasks SET {assignments}, updated_at=? "  # nosec B608
                "WHERE id=? AND user_id=?",
                tuple(params),
            )
        return cursor.rowcount > 0


def init_storage(settings: JerichoSettings) -> JerichoStorage:
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    settings.files_dir.mkdir(parents=True, exist_ok=True)
    settings.backups_dir.mkdir(parents=True, exist_ok=True)
    return JerichoStorage(settings)
