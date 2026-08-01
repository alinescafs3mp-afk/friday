"""Module-level foundations shared by the storage mixins.

Constants, schema DDL, exceptions and free helpers live here so that a mixin module
can use them without importing ``jericho.storage`` — which imports the mixins, and
would be a cycle. ``jericho.storage`` re-exports the public names, so every existing
``from jericho.storage import ...`` keeps working.
"""

from __future__ import annotations

# Explicit, because the mixins import these FROM here: without __all__ a lint
# autofix reads them as unused in this module and deletes them.
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
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from datetime import UTC, datetime, timedelta
from difflib import SequenceMatcher
from pathlib import Path, PurePosixPath
from typing import Any

from jericho.config import JerichoSettings
from jericho.morphology import stem_to_fixpoint as fold_russian_word
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

# Named for the package, not this module: `__name__` here is "jericho.storage._base", and
# the split must not rename the logger operators already read in the logs.
LOGGER = logging.getLogger("jericho.storage")
SCHEMA_VERSION = 21
MAX_API_TOKEN_TTL_SECONDS = 100 * 365 * 24 * 3600
EVAL_MINED_CASE_CAP = 200
# Operational journal size. Roughly a month of transitions for this workload, and a
# hard bound on what the table can cost regardless of how badly something flaps.
RUNTIME_EVENT_CAP = 2000
_SEARCH_TEXT_LEN_SQL = (
    "length(coalesce(k.title,'') || coalesce(k.summary,'') || coalesce(k.content,'')"
    " || coalesce(k.tags_json,'') || coalesce(k.knowledge_kind,''))"
)


class UnsupportedSchemaVersionError(RuntimeError):
    """The database was created by an incompatible newer or corrupt schema."""


class SourceReferenceConflictError(ValueError):
    """An immutable source reference was reused for different content."""


class StorageClosedError(RuntimeError):
    """A database operation was attempted after the process shut its storage down.

    Loud on purpose. The ``conn`` property transparently reopens whenever the
    generation moved, which is the contract ``restore_backup`` relies on — and it
    meant a worker thread that outlived shutdown got a **brand new connection**
    instead of an error, and went on writing after the process had already released
    its ``backend.lock``. At that point a replacement backend may hold the lease.
    """


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

-- Кем человек ВОШЁЛ против того, ЧЬИ это данные. До этой таблицы одно было равно
-- другому: телеграм-аккаунт получал идентификатор `telegram:{realm}:{id}`, и он же
-- служил арендатором. Следствие было незаметным и полным: владелец, импортировавший
-- корпус через CLI, и он же, спрашивающий в телеграме, — два РАЗНЫХ арендатора.
-- Поиск ограничен арендатором, поэтому вопрос из телеграма физически не мог найти
-- собственные документы владельца, и система честно отвечала «ничего не нашлось».
--
-- Связь заводится ЯВНО. Автоматически привязывать входящую личность к существующему
-- аккаунту нельзя ни по имени, ни по номеру: это выдача доступа к чужим данным тому,
-- кто всего лишь написал боту.
CREATE TABLE IF NOT EXISTS user_identities (
    source TEXT NOT NULL,
    external_id TEXT NOT NULL,
    user_id TEXT NOT NULL REFERENCES users(id),
    linked_by TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    PRIMARY KEY (source, external_id)
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
    -- What the merge actually moved. Without this, INSERT OR IGNORE on links
    -- makes overlapping documents unrecoverable: after the merge there is one
    -- row and no way to know whose it was. Empty history on live installs when
    -- this column shipped, so no backfill of old rows is required.
    transfer_json TEXT NOT NULL DEFAULT '{}',
    merged_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    undone_at TEXT,
    undone_by TEXT
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
    chunk_scheme TEXT NOT NULL DEFAULT '',
    vector BLOB NOT NULL,
    updated_at TEXT NOT NULL
);

-- Passage-level vectors for long Knowledge Objects. ``knowledge_embeddings`` keeps
-- exactly one whole-object vector per KO (near-duplicate detection and coverage
-- counts depend on that), and that vector stays the FLOOR of dense recall: chunk
-- rows can only raise an object's score, never lower it. Rows exist only for
-- objects the chunker actually split. ``start_char``/``end_char`` are offsets into
-- ``knowledge_objects.content`` so the winning passage can ground the answer.
CREATE TABLE IF NOT EXISTS knowledge_chunk_embeddings (
    knowledge_object_id TEXT NOT NULL REFERENCES knowledge_objects(id),
    chunk_index INTEGER NOT NULL,
    user_id TEXT NOT NULL REFERENCES users(id),
    model TEXT NOT NULL,
    dim INTEGER NOT NULL,
    source_version INTEGER NOT NULL DEFAULT 0,
    chunk_scheme TEXT NOT NULL DEFAULT '',
    start_char INTEGER NOT NULL DEFAULT 0,
    end_char INTEGER NOT NULL DEFAULT 0,
    content_hash TEXT NOT NULL DEFAULT '',
    vector BLOB NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(knowledge_object_id, chunk_index)
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

-- Мониторы: сохранённый вопрос, за которым система следит сама (спека v3 §6,
-- «monitors evaluate explicit conditions and produce deduplicated notifications»).
--
-- Условие хранится как ТЕКСТ ЗАПРОСА, а не как выражение на своём языке: у
-- проекта уже есть один поиск, и второй язык условий означал бы вторую
-- реализацию «что считается совпадением» — она бы разошлась с первой молча.
--
-- `last_seen_rowid` — граница «что уже показывали». Именно rowid, а не время:
-- `utc_now()` здесь секундной точности, и документ, пришедший в ТУ ЖЕ секунду,
-- что и создание монитора, при сравнении по времени терялся бы навсегда — а по
-- курсору он честно оказывается «после». Тот же приём, что у проходов по корпусу
-- (`knowledge_bodies_after`). Время рядом хранится для показа человеку.
CREATE TABLE IF NOT EXISTS monitors (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id),
    query TEXT NOT NULL,
    chat_id TEXT NOT NULL DEFAULT '',
    active INTEGER NOT NULL DEFAULT 1,
    last_seen_rowid INTEGER NOT NULL DEFAULT 0,
    last_seen_at TEXT NOT NULL,
    last_checked_at TEXT,
    matches_reported INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_monitors_user ON monitors(user_id, active);

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
CREATE INDEX IF NOT EXISTS idx_user_identities_user ON user_identities(user_id);
CREATE INDEX IF NOT EXISTS idx_raw_objects_user_received ON raw_objects(user_id, received_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS uq_raw_source_ref
    ON raw_objects(user_id, source, source_ref) WHERE source_ref <> '';
CREATE INDEX IF NOT EXISTS idx_knowledge_user_lifecycle
    ON knowledge_objects(user_id, lifecycle_stage, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_knowledge_user_quality
    ON knowledge_objects(user_id, quality_score DESC, promotion_score DESC, importance DESC);
-- The recall pool's own order. `idx_knowledge_user_quality` starts with user_id, so
-- SQLite used it to FIND the rows and then sorted every one of them in a temp
-- b-tree — importance is its fourth column, which orders nothing here. Measured at
-- 10k objects: 90.9 ms for a 400-row page. Partial on `deleted_at IS NULL` because
-- every caller filters on it, which also makes it serve `count_knowledge_objects`.
CREATE INDEX IF NOT EXISTS idx_knowledge_user_importance
    ON knowledge_objects(user_id, importance DESC, updated_at DESC) WHERE deleted_at IS NULL;
-- Тот же порядок плюс уникальный хвост. `importance` и `updated_at` пишутся с
-- точностью до секунды, и один импорт ставит сотням строк один и тот же ключ —
-- без хвоста SQLite вправе вернуть их в разном порядке двум соседним запросам,
-- то есть строка задваивается на одной границе страницы и пропадает на другой.
--
-- Раньше это считалось неисправимым: добавить `id DESC` в ORDER BY при СТАРОМ
-- индексе стоило 90 мс на 10k. Замер показал, что оценка была неверной по месту.
-- SQLite не пересортировывает весь пул, он даёт `USE TEMP B-TREE FOR LAST TERM
-- OF ORDER BY`, и на первой странице это +0…8 мс. Настоящая цена — на глубоких
-- страницах: досортировка ломает пропуск офсета по индексу, и SQLite тащит в
-- сортировщик все пропускаемые строки целиком (`SELECT *`). Замер 10k, пачечный
-- импорт, offset 4000: 9.9 мс → 145.2 мс. А офсетные проходы по всему пулу
-- реальны — `ingestion/_legacy.py` листает его батчами по 500.
--
-- С этим индексом хвост бесплатен и вдобавок ускоряет прежний запрос: тот же
-- offset 4000 даёт 5.4 мс. Старый трёхколоночный индекс остаётся строгим
-- префиксом этого; удалить его можно только кодом миграции (CORE_INDEX_SCHEMA
-- умеет лишь CREATE), поэтому он живёт дальше и стоит одну запись на вставку.
-- Имя намеренно НЕ начинается с `idx_knowledge_user_importance`: тест, который
-- закрепляет план, проверяет вхождение подстроки, и любое имя с этим префиксом
-- проходило бы его вхолостую — даже когда план уже переехал на другой индекс.
CREATE INDEX IF NOT EXISTS idx_knowledge_pool_order
    ON knowledge_objects(user_id, importance DESC, updated_at DESC, id DESC) WHERE deleted_at IS NULL;
-- Dense recall caps its scan to the newest N objects, and the sort key lives on the
-- joined table, so the LIMIT could not short-circuit: every vector BLOB of the tenant
-- was read into a temp b-tree first. Measured at 10k vectors: 469 ms.
CREATE INDEX IF NOT EXISTS idx_knowledge_user_created
    ON knowledge_objects(user_id, created_at DESC) WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_knowledge_raw ON knowledge_objects(raw_object_id);
-- Запасной путь `get_entity_knowledge` ищет объекты по прямой колонке `entity_id`,
-- когда у сущности нет принятых связей. Индекса на неё не было, поэтому запрос
-- сканировал ВСЕ объекты арендатора, вытаскивая `content` каждого. Замерено на
-- 5000 сущностей и 5000 объектов: 58.4 мс на вызов, а `GET /kg/resolutions/pending`
-- зовёт его дважды на кандидата — 317.5 секунды на ответ, из них 99.6% здесь.
-- Ровно этот путь чаще всего и берётся: кандидаты на слияние — свежие тонкие
-- сущности, у которых принятых связей ещё нет.
-- Колонки сортировки входят в индекс НАМЕРЕННО, и это не украшение: с индексом
-- только по (user_id, entity_id) план не менялся вовсе — SQLite предпочитал
-- `idx_knowledge_user_importance`, потому что тот обслуживает ORDER BY, и
-- продолжал сканировать всего арендатора. Замерено: 51.7 мс против 39.2 мс, то
-- есть индекс просто не использовался. С сортировкой внутри — **0.13 мс**.
CREATE INDEX IF NOT EXISTS idx_knowledge_user_entity
    ON knowledge_objects(user_id, entity_id, importance DESC, updated_at DESC)
    WHERE entity_id IS NOT NULL AND deleted_at IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_knowledge_version
    ON knowledge_object_versions(knowledge_object_id, version);
CREATE INDEX IF NOT EXISTS idx_inbox_user_status ON inbox(user_id, status, created_at DESC);
-- Source search resolves each FTS hit to its inbox verdict; without this it is a
-- scan of the whole inbox per hit.
CREATE INDEX IF NOT EXISTS idx_inbox_raw_status ON inbox(raw_object_id, status);
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
-- The composite primary key's implicit index already serves lookups and deletes by
-- knowledge_object_id (leftmost prefix), so only the scan path needs an index.
CREATE INDEX IF NOT EXISTS idx_knowledge_chunks_user_model
    ON knowledge_chunk_embeddings(user_id, model, dim);
-- Keyset order for the incremental near-duplicate scan: (updated_at, id) is a total
-- order that only ever moves UPWARD, because every vector write stamps updated_at.
CREATE INDEX IF NOT EXISTS idx_knowledge_embeddings_user_model_updated
    ON knowledge_embeddings(user_id, model, updated_at, knowledge_object_id);
CREATE INDEX IF NOT EXISTS idx_api_tokens_user ON api_tokens(user_id, revoked_at);
CREATE INDEX IF NOT EXISTS idx_entity_time_user ON entity_time(user_id, occurred_at);
CREATE INDEX IF NOT EXISTS idx_outbound_status ON outbound_notifications(status, created_at);
CREATE UNIQUE INDEX IF NOT EXISTS uq_outbound_dedup
    ON outbound_notifications(user_id, dedup_key) WHERE dedup_key <> '';
"""
CORE_INDEX_MARKER = "CREATE INDEX IF NOT EXISTS idx_users_source_external"
CORE_TABLE_SCHEMA, CORE_INDEX_SCHEMA_TAIL = CORE_SCHEMA.split(CORE_INDEX_MARKER, 1)
CORE_INDEX_SCHEMA = CORE_INDEX_MARKER + CORE_INDEX_SCHEMA_TAIL
# The vocabulary of the knowledge index, as a view over it: no second copy of any
# text, no triggers, nothing to keep in sync. It answers "is this a word this
# corpus uses?", which is what spelling repair must know before it dares replace
# a term the user typed.
#
# Applied SEPARATELY from FTS_SCHEMA on purpose: it is an optional convenience,
# and an SQLite build without the `fts5vocab` module must lose spelling repair
# rather than lose full-text search along with it.
FTS_VOCAB_SCHEMA = "CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_vocab USING fts5vocab(knowledge_fts, 'row');"
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

-- Source text, so an exact phrase from the original document is findable even when
-- the Knowledge Object kept only a summary. Measured on the owner's database: 93%
-- of ingested characters lived in `raw_objects` and no index covered them.
--
-- Reachability is decided by the INBOX VERDICT at query time, never here — see
-- `search_raw_objects`. Indexing everything and filtering on read is deliberate:
-- a verdict can be revised (an ignored item returned to pending), and an index
-- that had silently dropped the row could not follow.
CREATE VIRTUAL TABLE IF NOT EXISTS raw_fts USING fts5(
    raw_content,
    content=raw_objects,
    content_rowid=rowid,
    tokenize='unicode61 remove_diacritics 2'
);

CREATE TRIGGER IF NOT EXISTS raw_objects_ai AFTER INSERT ON raw_objects BEGIN
    INSERT INTO raw_fts(rowid, raw_content) VALUES (new.rowid, new.raw_content);
END;

CREATE TRIGGER IF NOT EXISTS raw_objects_ad AFTER DELETE ON raw_objects BEGIN
    INSERT INTO raw_fts(raw_fts, rowid, raw_content) VALUES ('delete', old.rowid, old.raw_content);
END;

CREATE TRIGGER IF NOT EXISTS raw_objects_au AFTER UPDATE ON raw_objects BEGIN
    INSERT INTO raw_fts(raw_fts, rowid, raw_content) VALUES ('delete', old.rowid, old.raw_content);
    INSERT INTO raw_fts(rowid, raw_content) VALUES (new.rowid, new.raw_content);
END;

-- Chat history: mainstream assistants search the whole conversation, not only
-- knowledge_objects the user consciously saved. External-content FTS over
-- messages.content, same trigger contract as knowledge_fts / raw_fts.
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    content,
    content=messages,
    content_rowid=rowid,
    tokenize='unicode61 remove_diacritics 2'
);

CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, content) VALUES (new.rowid, new.content);
END;

CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, content) VALUES ('delete', old.rowid, old.content);
END;

CREATE TRIGGER IF NOT EXISTS messages_au AFTER UPDATE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, content) VALUES ('delete', old.rowid, old.content);
    INSERT INTO messages_fts(rowid, content) VALUES (new.rowid, new.content);
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


_CYRILLIC_SEGMENT = re.compile(r"[^\W\d_]+", re.UNICODE)


def _fold_russian(text: str) -> str:
    """Stem every wholly-Cyrillic word inside ``text``, leave the rest untouched.

    Applied per WORD rather than to the whole string so a mixed name keeps the
    part that is a code: «CIDR-ПОДПИСКА» and «CIDR-ПОДПИСКУ» become one node
    while `PK-04-04`, `BRK.A`, `GPL-3.0` and `Tor Project` are byte-for-byte
    unchanged. Measured before it shipped, on the owner's own graph: exactly two
    collisions, both of them the known duplicate pairs, and zero false merges —
    47 stand entities and 30 live ones, nothing else moved.
    """
    return _CYRILLIC_SEGMENT.sub(
        lambda match: (
            fold_russian_word(match.group(0))
            if all("а" <= character <= "я" or character == "ё" for character in match.group(0))
            else match.group(0)
        ),
        text,
    )


def normalize_entity_name(value: str) -> str:
    """Normalize entity names while preserving exact identifiers and contract codes.

    Human names are whitespace/punctuation normalized for useful alias lookup.  Compact
    identifiers keep dots, slashes, dashes, and suffixes so distinct symbols do not collapse.

    Russian inflection is folded away, because a graph node is a THING and «Пётр
    Иванов» is not a different person from «Петра Иванова». Without this the same
    subject accumulates one node per grammatical case: the owner's graph held
    «CIDR-ПОДПИСКА» beside «CIDR-ПОДПИСКУ», and a node that exists twice can be
    neither reliably found nor reliably counted. `ё` folds for the same reason —
    it is the same letter to a reader and the phone keyboard does not type it.

    This is the stored `entities.normalized_name`, so changing it is a schema
    migration (18) that recomputes the column. Existing nodes are NOT merged:
    folding makes them resolve to one another going forward and makes the pair
    visible as a duplicate candidate, while merging stays the owner's decision.
    """

    raw = unicodedata.normalize("NFKC", value or "").strip()
    if _is_entity_identifier(raw):
        return _fold_russian(raw.casefold().replace("ё", "е"))
    text = raw.casefold().replace("ё", "е")
    text = re.sub(r"[\s\-_./]+", " ", text)
    text = re.sub(r"[^\w\s#+&]", "", text, flags=re.UNICODE)
    return _fold_russian(" ".join(text.split()))


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


# Сжатый снимок версии: магический префикс отличает наши байты от текста JSON.
# Каждый снапшот несёт ПОЛНЫЙ content, чистки не существовало нигде — массовое
# ре-обогащение добавляло копию корпуса в базу навсегда и раздувало каждый из
# хранимых суточных бэкапов. Полными держатся N последних версий объекта
# (откат и diff почти всегда к ним), старшие сжимаются zlib (~3-5x на русском
# JSON); читатели распаковывают прозрачно, так что откат жив к ЛЮБОЙ версии.
_SNAPSHOT_MAGIC = b"zKOV1"


def pack_snapshot(text: str) -> bytes:
    import zlib

    return _SNAPSHOT_MAGIC + zlib.compress(text.encode("utf-8"), level=6)


def unpack_snapshot(value: Any) -> str:
    """Текст снимка из хранимого значения — сжатого или прежнего текстового."""
    if isinstance(value, bytes):
        if value.startswith(_SNAPSHOT_MAGIC):
            import zlib

            return zlib.decompress(value[len(_SNAPSHOT_MAGIC) :]).decode("utf-8")
        return value.decode("utf-8", errors="replace")
    return str(value or "")


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


class StorageShared:
    """What a mixin may rely on its siblings providing.

    The repository is assembled from mixins, so within one module the connection
    plumbing and the handful of cross-domain calls live on a class the type checker
    cannot see. Declaring them here — as annotations, so nothing is actually defined
    and no method is shadowed — keeps the checking honest instead of silencing it.
    The real guarantee that they exist is ``tests/test_storage_surface.py``.
    """

    settings: JerichoSettings
    execute: Callable[..., sqlite3.Cursor]
    transaction: Callable[..., Any]
    close: Callable[..., None]
    conn: Any
    ensure_user: Callable[..., dict[str, Any]]
    # Курсор обхода дедупа сущностей живёт в runtime_kv — общая таблица, своя
    # миксина. Объявлено здесь, как и остальные межмиксинные вызовы.
    kv_get: Callable[..., str | None]
    kv_set: Callable[..., None]
    get_user: Callable[..., dict[str, Any] | None]
    get_raw_object: Callable[..., Any]
    get_entity: Callable[..., dict[str, Any] | None]
    list_entities: Callable[..., list[dict[str, Any]]]
    count_entities: Callable[..., int]
    find_entities_by_normalized_names: Callable[..., list[dict[str, Any]]]
    get_knowledge_object: Callable[..., dict[str, Any] | None]
    list_eval_cases: Callable[..., list[dict[str, Any]]]
    eval_case_health: Callable[..., dict[str, Any]]
    _db_path: Path
    _schema_ready: bool
    _connections: Any
    _generation: int
    _write_lock: Any
    _registry_lock: Any
    _init_lock: Any
    _local: Any
    _fts_available: bool
    _BRIDGE_NONCE_PREFIX: str
    _MISSION_UPDATABLE: frozenset[str]
    _MISSION_TASK_UPDATABLE: frozenset[str]


__all__ = [
    "Any",
    "AuditEntry",
    "CONVERSATION_MODES",
    "CORE_INDEX_MARKER",
    "CORE_INDEX_SCHEMA",
    "CORE_INDEX_SCHEMA_TAIL",
    "CORE_SCHEMA",
    "CORE_TABLE_SCHEMA",
    "Callable",
    "EVAL_MINED_CASE_CAP",
    "RUNTIME_EVENT_CAP",
    "Entity",
    "EntityResolutionCandidate",
    "EntityType",
    "FTS_SCHEMA",
    "FeedbackItem",
    "InboxItem",
    "InboxStatus",
    "Iterator",
    "JerichoSettings",
    "KnowledgeObject",
    "LOGGER",
    "LifecycleStage",
    "MAX_API_TOKEN_TTL_SECONDS",
    "Mapping",
    "Mission",
    "MissionStatus",
    "MissionTask",
    "Path",
    "PurePosixPath",
    "RawObject",
    "Relation",
    "RelationType",
    "ResolutionStatus",
    "SCHEMA_VERSION",
    "Sequence",
    "SequenceMatcher",
    "SourceReferenceConflictError",
    "StorageShared",
    "UTC",
    "UnsupportedSchemaVersionError",
    "_ENTITY_IDENTIFIER_RE",
    "_JSON_COLUMNS",
    "_SEARCH_TEXT_LEN_SQL",
    "_USER_ID_RE",
    "_chmod_private",
    "_fsync_directory",
    "_is_entity_identifier",
    "_json_load",
    "_safe_filename",
    "_sha256_file",
    "_snapshot",
    "_stage_private_copy",
    "_write_json_atomic",
    "_write_recovery_bundle",
    "annotations",
    "contextmanager",
    "datetime",
    "enum_value",
    "hashlib",
    "hmac",
    "json",
    "logging",
    "math",
    "new_id",
    "normalize_conversation_mode",
    "normalize_entity_name",
    "os",
    "re",
    "shutil",
    "sqlite3",
    "suppress",
    "tempfile",
    "threading",
    "time",
    "timedelta",
    "unicodedata",
    "utc_now",
    "validate_user_id",
]
