"""Module-level foundations shared by the storage mixins.

Constants, schema DDL, exceptions and free helpers live here so that a mixin module
can use them without importing ``friday.storage`` — which imports the mixins, and
would be a cycle. ``friday.storage`` re-exports the public names, so every existing
``from friday.storage import ...`` keeps working.
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

from friday.config import FridaySettings
from friday.morphology import stem_to_fixpoint as fold_russian_word
from friday.private_fs import ensure_private_directory
from friday.storage.models import (
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
from friday.user_ids import USER_ID_RE as _USER_ID_RE
from friday.user_ids import validate_user_id

_AUDIT_GENERATED_ID_LOCATIONS: dict[str, tuple[tuple[str, str], ...]] = {
    "apr": (("action_approvals", "id"),),
    "audit": (("audit_log", "id"),),
    "cmp": (("day_compacts", "id"),),
    "conf": (("knowledge_conflicts", "id"),),
    "conv": (("conversations", "id"),),
    "ent": (("entities", "id"),),
    "entv": (("entity_versions", "id"),),
    "er": (("entity_resolution_candidates", "id"),),
    "eval": (("eval_cases", "id"),),
    "evt": (("runtime_events", "id"),),
    "fb": (("feedback", "id"),),
    "feedback": (("feedback", "id"),),
    "inbox": (("inbox", "id"),),
    "kel": (("knowledge_entity_links", "id"),),
    "ko": (("knowledge_objects", "id"),),
    "kov": (("knowledge_object_versions", "id"),),
    "merge": (("entity_merge_history", "id"),),
    "mis": (("missions", "id"),),
    "mon": (("monitors", "id"),),
    "msg": (("messages", "id"),),
    "msn": (("missions", "id"),),
    "mtask": (("mission_tasks", "id"),),
    "notif": (("outbound_notifications", "id"),),
    "raw": (("raw_objects", "id"),),
    "rel": (("relations", "id"),),
    "relation_batch": (("relation_revisions", "batch_id"),),
    "relc": (("relation_candidates", "id"),),
    "tok": (("api_tokens", "id"),),
}


def audit_generated_id_exists(
    execute: Callable[[str, tuple[Any, ...]], Any],
    candidate: str,
    prefixes: frozenset[str],
) -> bool:
    """Prove that a serialized generated ID already belongs to this database."""

    prefix = candidate.rsplit("_", 1)[0]
    if prefix not in prefixes:
        return False
    for table, column in _AUDIT_GENERATED_ID_LOCATIONS.get(prefix, ()):
        # Table/column names come only from the closed constant above.
        try:
            row = execute(
                f'SELECT 1 FROM "{table}" WHERE "{column}"=? LIMIT 1',  # nosec B608
                (candidate,),
            ).fetchone()
        except sqlite3.OperationalError:
            continue
        if row is not None:
            return True
    return False


# Named for the package, not this module: `__name__` here is "friday.storage._base", and
# the split must not rename the logger operators already read in the logs.
LOGGER = logging.getLogger("friday.storage")
# 25 — столбец `monitors.created_by` (автор слежения).
#
# Номер обязан расти вместе с добавлением столбца, и это не формальность:
# `_migrate_legacy_schema` вызывается ТОЛЬКО когда отметка в базе меньше этого
# числа. Проверено на живой базе 2026-08-04 — со старым номером столбец не
# появился, а код его уже читал, и маршрут слежений отдавал 500. Тесты этого не
# видели: там база создаётся с нуля по актуальному CREATE TABLE.
# 30 — `uq_active_relation` теперь уникален только для ДЕЙСТВУЮЩЕГО интервала.
# Завершённая связь остаётся историей и не должна запрещать новый период той же
# пары/типа. Старый индекс пересоздаётся в `_retire_outdated_indexes`.
# 31 — append-only transaction-time история relations. Mutable `relations`
# остаётся быстрой текущей проекцией, а триггеры фиксируют каждое её состояние.
# 32 — monotonic observed boundary и REPLACE/conflict guards для этой истории.
# Это отдельный upgrade: выпущенную schema 31 нельзя молча чинить под тем же
# marker, иначе current-schema fail-closed validator примет миграцию за tampering.
# 33 — immutable aliases for file transport identities. Content deduplication
# deliberately reuses one Raw Object, but every successful Telegram re-upload
# still needs its fresh file_id to resolve back to that canonical object.
# 34 — the immutable transport alias also preserves the bounded basename that
# arrived with that exact carrier. Dedup may keep an older canonical Raw name;
# later exact filename lookup must still find the name the user actually sent.
# 35 — isolated Syncthing/Obsidian identity, onboarding, operation-delivery and
# conflict records. Note bodies remain in the contained per-user vault checkout;
# API keys remain in private Syncthing config files and never enter SQLite.
# 36 — stable Obsidian note bindings, revision-aware lexical/metadata projection,
# link graph and expiring continuation state.  The released schema-35 operation
# table is rebuilt only after its exact old shape has passed fail-closed validation.
# 37 — bounded person-owned interaction failures which occur before an assistant
# message can own the ordinary Turn Trace.  Bodies and exception text are excluded;
# account/conversation deletion can remove the dedicated structural rows directly.
# 38 — lightweight person-owned interactive Work Items and their bounded Active
# Frame.  The first slice persists only RecallConversation control state; bodies
# remain in messages, while exact DDL validation and revision CAS make restart and
# continuation state fail closed.
# 39 — one body-free selected-evidence sidecar and the closed
# RecallSelectedArchiveEvidence workflow labels.  The released schema-38 table is
# authenticated and rebuilt without changing any existing RecallConversation row.
# 40 — closed body-free archive candidate sets and typed ordinal questions. Exact
# schema-38/39 projections are authenticated before their Work Item tables rebuild.
# 41 — durable body-free DocumentCatalog projection. Raw Object remains authority;
# exact revision guards and explicit incomplete states make bounded backfill honest.
# 42 — dormant reader-first conversation/document Work Item projection.  Exact
# message selection, Raw document pins, ambiguity history and accepted completion
# receipts survive restart without persisting prompts, paths, titles or bodies.
# 43 — durable immutable host-action plans, restart-safe lifecycle state and an
# append-only event chain for the optional native host capability plane.
# 44 — dormant journey-specific current-file/current-web WorkGraph.  The fixed
# two-read/one-primary-synthesis topology and body-free CAS state ship without a
# worker, adapter invocation, model execution or publication route.
# 45 — immutable body-free ingress request binding for the assist WorkGraph.
# Released schema-44 graphs migrate with an explicit unbound sentinel; no
# request body, source reference or inferred replay identity is manufactured.
# 46 — dormant journey-specific EngineerWorkItem v1.  Only exact owner/source
# bindings, lifecycle state and opaque receipt digests persist; activation and
# model-loop continuation remain a later, independently reversible package.
# 47 — reader-first body-free document passage projections. Raw remains the
# authority; this release seeds only explicit incomplete state so the same
# schema-capable binary can safely serve as fallback for later bounded writers.
# 48 — document-only passage v3 filters released contained/non-progress spans.
# Exact schema-47 v2 DDL/data are authenticated before only the rebuildable
# passage tables advance; unchanged CURRENT topology is carried byte-for-byte,
# while changed topology returns to explicit bounded-writer work.
# 49 — reader-first body-free conversation-passage anchors.  Historical
# conversations start explicitly backfill_pending; the released schema already
# accepts future authenticated CURRENT rows so it can be sealed as the rollback
# binary before the independently reversible writer/search activation.
SCHEMA_VERSION = 49

#: Определение таблицы внешних источников отдельной константой: миграция схемы 29
#: пересоздаёт её, чтобы ключом стала ПАРА `(user_id, name)`, и должна брать ровно
#: это определение. Копия внутри `CORE_SCHEMA` обязана совпадать — за этим следит
#: `tests/test_one_table_one_definition.py`.
DATA_SOURCES_SCHEMA = """
CREATE TABLE IF NOT EXISTS data_sources (
    name TEXT NOT NULL,
    user_id TEXT NOT NULL REFERENCES users(id),
    kind TEXT NOT NULL,
    dsn_env TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL DEFAULT '',
    last_used_at TEXT,
    -- Ключ — ПАРА, а не имя. Имя было первичным ключом в схеме 28, и это
    -- пробивало границу арендаторов: читается источник всегда парой
    -- `name + user_id`, а писался по имени, поэтому объявление «hr» вторым
    -- человеком делало UPDATE чужой строки, оставляя в ней прежнего владельца.
    -- Чужой источник начинал смотреть в переменную окружения соседа.
    PRIMARY KEY(user_id, name)
);
"""

#: Определение таблицы шагов миссии отдельной константой: миграция схемы 24
#: пересоздаёт её, чтобы расширить список состояний, и должна брать ровно это
#: определение, а не свою копию — иначе две формы таблицы разойдутся.
MISSION_TASKS_SCHEMA = """
CREATE TABLE IF NOT EXISTS mission_tasks (
    id TEXT PRIMARY KEY,
    mission_id TEXT NOT NULL REFERENCES missions(id),
    user_id TEXT NOT NULL REFERENCES users(id),
    seq INTEGER NOT NULL,
    kind TEXT NOT NULL DEFAULT 'gather' CHECK(kind IN ('gather', 'produce')),
    title TEXT NOT NULL DEFAULT '',
    instruction TEXT NOT NULL,
    depends_on_json TEXT NOT NULL DEFAULT '[]',
    -- `uncertain` и `compensated` — состояния восстановления после сбоя рядом с
    -- побочным эффектом (спека v3 §5). `uncertain` значит «неизвестно, случился
    -- ли эффект»: такой шаг требует сверки с миром, а не повтора вслепую.
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK(status IN ('pending', 'running', 'done', 'failed', 'skipped',
                         'uncertain', 'compensated')),
    attempts INTEGER NOT NULL DEFAULT 0,
    -- Шаг с побочным эффектом: для него важны чекпойнт и компенсация.
    side_effect INTEGER NOT NULL DEFAULT 0,
    checkpoint_json TEXT NOT NULL DEFAULT '{}',
    compensation TEXT NOT NULL DEFAULT '',
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
"""
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


class PrivateMaterialQuarantineError(ValueError):
    """Текст ссылается на ЧУЖОЙ приватный материал и потому не сохраняется.

    Отказ штатный: такой объект был бы невидим каждому читателю, включая того,
    кто его прислал, и молча пропал бы. Имя у ошибки появилось потому, что голый
    `ValueError` доезжал до человека как `HTTP 500` без единой строки в журнале:
    `install_external_exception_privacy` снимает traceback с `uvicorn.error`, и
    оператор не мог узнать даже того, что отказ вообще произошёл.
    """


class DeletedAccountError(ValueError):
    """An explicitly erased account identifier must never be provisioned again.

    Hard deletion removes every access row, including the identity link that used
    to lead here.  Without a durable tombstone, the next Telegram update would
    derive the same account id and ``ensure_user`` would silently recreate it.  A
    hash-keyed row in ``runtime_kv`` keeps the identifier unavailable without
    retaining another live account row.
    """


class StorageClosedError(RuntimeError):
    """A database operation was attempted after the process shut its storage down.

    Loud on purpose. The ``conn`` property transparently reopens whenever the
    generation moved, which is the contract ``restore_backup`` relies on — and it
    meant a worker thread that outlived shutdown got a **brand new connection**
    instead of an error, and went on writing after the process had already released
    its ``backend.lock``. At that point a replacement backend may hold the lease.
    """


DELETED_ACCOUNT_TOMBSTONE_PREFIX = "deleted_account:v1:"
DELETED_IDENTITY_TOMBSTONE_PREFIX = "deleted_identity:v1:"
ACCOUNT_DELETION_ELIGIBILITY_PREFIX = "account_deletion_eligible:v1:"
ACCOUNT_EXTERNAL_IDENTITY_HISTORY_PREFIX = "account_external_identity_history:v1:"
_ACCOUNT_RUNTIME_EXACT_USER_PREFIXES = (
    "dedup:scan:",
    "entity_dedup:cursor:",
    "eval:ablation:",
    "eval:chunk_ab:",
    "eval:last_report:",
    "eval:last_run:",
    "graph:mention_backfill:",
    "workers:knowledge_quality:",
    "workers:lifecycle:",
)
_ACCOUNT_RUNTIME_LENGTH_NAMESPACES = ("candidate", "present", "validation", "winner")
_ACCOUNT_RUNTIME_QUOTA_NAMES = ("web",)
CONVERSATION_MODES = {"dialogue", "knowledge_work", "research", "engineer"}


def normalize_identity_source(source: str) -> str:
    """Canonical authority key for an external identity provider name."""

    value = unicodedata.normalize("NFC", str(source or "").strip()).casefold()
    return unicodedata.normalize("NFC", value)


def deleted_account_tombstone_key(user_id: str) -> str:
    """Opaque durable key which prevents a hard-deleted id from reappearing."""

    canonical = validate_user_id(user_id)
    digest = hashlib.sha256(("friday-deleted-account\0" + canonical).encode("utf-8")).hexdigest()
    return DELETED_ACCOUNT_TOMBSTONE_PREFIX + digest


def deleted_identity_tombstone_key(source: str, external_id: str) -> str:
    """Opaque durable key which prevents a revoked login from being reattached."""

    canonical_source = normalize_identity_source(source)
    canonical_external_id = str(external_id or "").strip()
    if not canonical_source or not canonical_external_id:
        raise ValueError("source and external_id are required")
    material = f"friday-deleted-identity\0{canonical_source}\0{canonical_external_id}"
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
    return DELETED_IDENTITY_TOMBSTONE_PREFIX + digest


def account_deletion_eligibility_key(user_id: str) -> str:
    """Opaque proof that this account was born inside the coordinated contour."""

    canonical = validate_user_id(user_id)
    digest = hashlib.sha256(("friday-account-deletion-eligibility\0" + canonical).encode("utf-8")).hexdigest()
    return ACCOUNT_DELETION_ELIGIBILITY_PREFIX + digest


def account_external_identity_history_key(user_id: str) -> str:
    """Opaque, irreversible marker for external/Telegram account history."""

    canonical = validate_user_id(user_id)
    digest = hashlib.sha256(
        ("friday-account-external-identity-history\0" + canonical).encode("utf-8")
    ).hexdigest()
    return ACCOUNT_EXTERNAL_IDENTITY_HISTORY_PREFIX + digest


def known_runtime_key_owners(key: str) -> set[str]:
    """Decode owners only for closed, code-written account runtime formats."""

    def valid_owner(value: str) -> str | None:
        try:
            return validate_user_id(value)
        except ValueError:
            return None

    root = "graph:mention_backfill:"
    for namespace in _ACCOUNT_RUNTIME_LENGTH_NAMESPACES:
        prefix = f"{root}{namespace}:"
        if not key.startswith(prefix):
            continue
        owners: set[str] = set()
        simple_owner = valid_owner(key[len(root) :])
        if simple_owner:
            owners.add(simple_owner)
        remainder = key[len(prefix) :]
        length_text, separator, payload = remainder.partition(":")
        if separator and len(length_text) == 8 and length_text.isdigit():
            owner_length = int(length_text)
            owner = payload[:owner_length]
            if payload[owner_length : owner_length + 1] == ":":
                structured_owner = valid_owner(owner)
                if structured_owner:
                    owners.add(structured_owner)
        return owners

    for name in _ACCOUNT_RUNTIME_QUOTA_NAMES:
        match = re.fullmatch(rf"quota:{re.escape(name)}:(.+):(\d{{4}}-\d{{2}}-\d{{2}})", key)
        if match:
            quota_owner = valid_owner(match.group(1))
            return {quota_owner} if quota_owner else set()

    for prefix in _ACCOUNT_RUNTIME_EXACT_USER_PREFIXES:
        if key.startswith(prefix):
            exact_prefix_owner = valid_owner(key[len(prefix) :])
            return {exact_prefix_owner} if exact_prefix_owner else set()
    return set()


def normalize_conversation_mode(mode: str | None) -> str:
    value = str(mode or "dialogue").strip().casefold().replace("-", "_")
    aliases = {
        "chat": "dialogue",
        "work": "knowledge_work",
        "knowledge": "knowledge_work",
        "engeneer": "engineer",
    }
    value = aliases.get(value, value)
    if value not in CONVERSATION_MODES:
        raise ValueError("mode must be dialogue, knowledge_work, research, or engineer")
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

-- One transport identity may point at an already-existing immutable Raw Object
-- after byte-level deduplication.  Keep that fact separately: changing Raw
-- source_ref would rewrite its original provenance, while dropping the fresh
-- Telegram file_id makes a later reply-to-file resolve to an unrelated history
-- pointer.  Uploader is part of the key because shared archives deliberately
-- share user_id while retaining private conversational ownership.
CREATE TABLE IF NOT EXISTS file_source_aliases (
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    uploaded_by TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    source_ref TEXT NOT NULL,
    raw_object_id TEXT NOT NULL REFERENCES raw_objects(id) ON DELETE CASCADE,
    supplied_filename TEXT NOT NULL DEFAULT ''
        CHECK(length(supplied_filename) <= 260
              AND instr(supplied_filename, '/') = 0
              AND instr(supplied_filename, '\\') = 0
              AND instr(supplied_filename, char(0)) = 0
              AND instr(supplied_filename, char(10)) = 0
              AND instr(supplied_filename, char(13)) = 0
              AND (substr(source_ref,1,20) <> 'friday-message-name:'
                   OR supplied_filename <> '')
              AND (supplied_filename = ''
                   OR substr(source_ref,1,14) = 'telegram-file:'
                   OR (length(source_ref) = 40
                       AND substr(source_ref,1,24) = 'friday-message-name:msg_'
                       AND substr(source_ref,25,16) NOT GLOB '*[^0-9a-f]*'))),
    created_at TEXT NOT NULL,
    PRIMARY KEY (user_id, uploaded_by, source_ref)
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
    -- Когда связь БЫЛА ВЕРНА, в отличие от `created_at` — когда мы о ней узнали.
    -- Это разные вещи, и путать их дорого: «служит в в/ч 30926» с рапорта 2024
    -- года остаётся фактом о 2024-м, даже если человек давно переведён. Пустая
    -- строка означает «начало неизвестно» — так заполняются связи, созданные до
    -- появления этих столбцов, и это честнее, чем выдать дату записи за дату
    -- события.
    valid_from TEXT NOT NULL DEFAULT '',
    -- NULL = связь действует до сих пор. Дата = перестала быть верной тогда-то.
    valid_to TEXT,
    -- Когда МЫ УЗНАЛИ, что связь недействительна. Второе время пары: valid_to
    -- отвечает «когда перестало быть правдой», invalidated_at — «когда мы это
    -- записали». Без второго нельзя ответить, что система знала на прошлой неделе.
    invalidated_at TEXT,
    -- Какая связь пришла на смену: «служит в в/ч А» → «служит в в/ч Б».
    superseded_by TEXT,
    CHECK(source_entity_id <> target_entity_id)
);

-- Один контекст на соединение с БД в каждый момент. Внешняя transaction() ставит
-- сюда общий timestamp/batch до relation DML и очищает перед тем же COMMIT. Пустой
-- контекст оставляет триггерам честный fallback для одиночного внешнего SQL.
-- observed_at — durable logical clock: уже выданный known_at и любой следующий
-- graph/identity commit становятся нижней границей для всех будущих записей.
CREATE TABLE IF NOT EXISTS relation_revision_context (
    singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
    batch_id TEXT NOT NULL DEFAULT '',
    recorded_at TEXT NOT NULL DEFAULT '',
    observed_at TEXT NOT NULL,
    CHECK(
        (batch_id = '' AND recorded_at = '')
        OR (batch_id <> '' AND recorded_at <> '')
    ),
    CHECK(observed_at <> '')
);

-- Каноническая append-only transaction-time история. Foreign keys здесь
-- намеренно нет: DELETE текущей проекции или endpoint не имеет права стирать либо
-- инвалидировать доказательство того, что система раньше считала правдой.
CREATE TABLE IF NOT EXISTS relation_revisions (
    event_seq INTEGER PRIMARY KEY AUTOINCREMENT,
    relation_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    present INTEGER NOT NULL CHECK(present IN (0, 1)),
    operation TEXT NOT NULL
        CHECK(operation IN ('insert', 'update', 'delete', 'migration_baseline')),
    recorded_at TEXT NOT NULL,
    batch_id TEXT NOT NULL,
    history_quality TEXT NOT NULL
        CHECK(history_quality IN ('captured', 'migration_baseline')),
    user_id TEXT NOT NULL,
    source_entity_id TEXT NOT NULL,
    target_entity_id TEXT NOT NULL,
    relation_type TEXT NOT NULL,
    weight REAL NOT NULL,
    metadata_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    deleted_at TEXT,
    valid_from TEXT NOT NULL,
    valid_to TEXT,
    invalidated_at TEXT,
    superseded_by TEXT,
    UNIQUE(relation_id, revision)
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

-- Redacting a legacy audit trail is a two-phase privacy migration: the rows are
-- rewritten first, then the old WAL is physically truncated.  While that
-- checkpoint is pending, even an old process must not append a row that the
-- retry path could accidentally certify as v3 without projecting it.
CREATE TRIGGER IF NOT EXISTS audit_log_privacy_pending_no_insert
BEFORE INSERT ON audit_log
WHEN EXISTS (
    SELECT 1 FROM schema_meta
     WHERE key='audit_payload_privacy' AND value='pending_wal_truncate:v3'
)
BEGIN
    SELECT RAISE(ABORT, 'audit privacy migration is pending');
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
    -- Бюджеты и срок живут В БАЗЕ, а не в памяти исполнителя: миссия обязана
    -- пережить перезапуск процесса вместе со своими ограничениями (спека v3 §5).
    -- Ноль означает «без ограничения» — так же, как отсутствующий срок.
    budget_seconds INTEGER NOT NULL DEFAULT 0,
    budget_tool_calls INTEGER NOT NULL DEFAULT 0,
    budget_retries INTEGER NOT NULL DEFAULT 0,
    spent_seconds INTEGER NOT NULL DEFAULT 0,
    spent_tool_calls INTEGER NOT NULL DEFAULT 0,
    spent_retries INTEGER NOT NULL DEFAULT 0,
    deadline_at TEXT,
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
    -- `uncertain` и `compensated` — состояния восстановления после сбоя рядом с
    -- побочным эффектом (спека v3 §5). `uncertain` значит «неизвестно, случился
    -- ли эффект»: такой шаг требует сверки с миром, а не повтора вслепую.
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK(status IN ('pending', 'running', 'done', 'failed', 'skipped',
                         'uncertain', 'compensated')),
    attempts INTEGER NOT NULL DEFAULT 0,
    -- Шаг с побочным эффектом: для него важны чекпойнт и компенсация.
    side_effect INTEGER NOT NULL DEFAULT 0,
    checkpoint_json TEXT NOT NULL DEFAULT '{}',
    compensation TEXT NOT NULL DEFAULT '',
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

-- Durable classification for person-owned graph rows.  `entity_time.source`
-- remains the scheduling provenance, but it can be replaced or lost when old
-- event rows are merged; privacy classification must survive both operations.
CREATE TABLE IF NOT EXISTS private_entity_owners (
    entity_id TEXT PRIMARY KEY REFERENCES entities(id) ON DELETE CASCADE,
    person_id TEXT NOT NULL DEFAULT '',
    privacy_kind TEXT NOT NULL CHECK(privacy_kind IN ('reminder')),
    created_at TEXT NOT NULL
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
    -- ЧЕЛОВЕК, а не арендатор. В общем архиве `user_id` один на всех, и без этого
    -- столбца «свои слежения» означали «все слежения»: участник читал в /watching
    -- чужие темы (текст запроса — личный интерес) и мог их снять.
    -- Найдено ревью 2026-08-04.
    created_by TEXT NOT NULL DEFAULT '',
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

-- Внешняя база как ИСТОЧНИК: Пятница ходит в неё за данными, но не переезжает
-- туда. Заказ владельца 2026-08-05.
--
-- ⚠️ Строки подключения здесь НЕТ и быть не должно. Хранится ИМЯ переменной
-- окружения (`dsn_env`), в которой она лежит. Причина не в аккуратности:
-- резервные копии этой базы лежат рядом с архивом и переживают всё, а экспорт
-- аккаунта отдаётся человеку целиком — пароль от чужой боевой базы уехал бы и
-- туда, и туда, и обнаружилось бы это не скоро.
CREATE TABLE IF NOT EXISTS data_sources (
    name TEXT NOT NULL,
    user_id TEXT NOT NULL REFERENCES users(id),
    kind TEXT NOT NULL,
    dsn_env TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL DEFAULT '',
    last_used_at TEXT,
    -- Ключ — ПАРА, а не имя. Имя было первичным ключом в схеме 28, и это
    -- пробивало границу арендаторов: читается источник всегда парой
    -- `name + user_id`, а писался по имени, поэтому объявление «hr» вторым
    -- человеком делало UPDATE чужой строки, оставляя в ней прежнего владельца.
    -- Чужой источник начинал смотреть в переменную окружения соседа.
    PRIMARY KEY(user_id, name)
);

-- Спека v3 §5: подтверждение опасного действия — durable, привязано к ТОЧНОМУ
-- нормализованному описанию действия и заявляется ровно один раз.
--
-- Модель — недоверенный источник предложений: сегодня она может сама слить две
-- сущности (`entity_merge_decide` accept), объявить знание устаревшим
-- (`conflict_decide`) или выполнить код. Право `kg.merge` отвечает на вопрос
-- «этому актору вообще можно», но не на вопрос «человек видел ИМЕННО ЭТО
-- действие и согласился».
--
-- `payload_hash` держит вторую половину: подтверждение годится только для того
-- набора аргументов, который человеку показали. Подмена аргументов после решения
-- не проходит проверку при заявлении.
--
-- Статусы различают исходы, которые спека требует различать при восстановлении
-- после падения: `claimed` — исполняется, `done`/`failed` — известный исход,
-- `uncertain` — процесс умер между заявлением и результатом, и повторять НЕЛЬЗЯ:
-- побочный эффект мог уже случиться. Такие записи ждут сверки человеком.
CREATE TABLE IF NOT EXISTS action_approvals (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id),
    tool TEXT NOT NULL,
    risk TEXT NOT NULL DEFAULT 'high' CHECK(risk IN ('mutate', 'high')),
    payload_json TEXT NOT NULL DEFAULT '{}',
    payload_hash TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK(status IN ('pending', 'approved', 'rejected', 'expired',
                         'claimed', 'done', 'failed', 'uncertain')),
    requested_by TEXT NOT NULL DEFAULT '',
    conversation_id TEXT,
    mission_id TEXT,
    policy_epoch TEXT NOT NULL DEFAULT '',
    decided_by TEXT,
    decided_at TEXT,
    claimed_at TEXT,
    result_json TEXT NOT NULL DEFAULT '{}',
    error TEXT NOT NULL DEFAULT '',
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_approvals_user_status
    ON action_approvals(user_id, status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_approvals_pending_expiry
    ON action_approvals(status, expires_at);

-- Ночная сводка о ПОВЕДЕНИИ системы за сутки. Заказ владельца 2026-08-04,
-- контракт — `docs/COMPACTOR.md`.
--
-- В таблице НЕТ НИ ОДНОЙ СТРОКИ, ВЫВЕДЕННОЙ ИЗ ПЕРЕПИСКИ, и это свойство схемы,
-- а не фильтра. Инцидент хранится перечислимым кодом, человеческая формулировка
-- рендерится при чтении из таблицы шаблонов в коде. Тогда утечке некуда попасть,
-- и «не просочилось ли имя» перестаёт быть надеждой на обезличивание.
--
-- Обоснование: обезличивание НЕ поручено модели. За двое суток пять раз
-- замерено, что промптовые ограничения не работают как механизм, а корпус
-- содержит фамилии, звания и названия подразделений.
--
-- `principal` — ЧЕЛОВЕК, не арендатор: в общем архиве корпус общий, а переписка
-- личная, и сводка о ней тоже. Тот же класс, что закрывался в правилах,
-- поправках и заявках на подтверждение.
--
-- UNIQUE(principal, local_date) — идемпотентность ПО ПОСТРОЕНИЮ: повторный
-- прогон за те же сутки делает UPSERT, и дубль создать просто нечем.
--
-- `status` — тот же приём, что у оборванных мутаторов: запись «начал» ставится
-- ДО работы, поэтому пара «начал / нет конца» сама доказывает незавершённость, и
-- следующий прогон видит, какой день переделать.
CREATE TABLE IF NOT EXISTS day_compacts (
    id TEXT PRIMARY KEY,
    principal TEXT NOT NULL REFERENCES users(id),
    local_date TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'started'
        CHECK(status IN ('started', 'done', 'uncertain')),
    source_turns INTEGER NOT NULL DEFAULT 0,
    counters_json TEXT NOT NULL DEFAULT '{}',
    incidents_json TEXT NOT NULL DEFAULT '[]',
    patterns_json TEXT NOT NULL DEFAULT '[]',
    started_at TEXT NOT NULL,
    finished_at TEXT,
    deleted_at TEXT,
    UNIQUE(principal, local_date)
);

CREATE INDEX IF NOT EXISTS idx_day_compacts_person
    ON day_compacts(principal, local_date DESC);

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
-- Поиск повтора по СОДЕРЖИМОМУ, когда ключ происхождения свежий на каждый вызов
-- (`find_fresh_agent_candidate`, `find_file_by_content_hash`). Частичный по живым
-- строкам: удалённые в этом поиске не участвуют никогда.
CREATE INDEX IF NOT EXISTS idx_raw_content_hash
    ON raw_objects(user_id, source, content_hash) WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_file_source_alias_raw
    ON file_source_aliases(user_id, raw_object_id);
CREATE TRIGGER IF NOT EXISTS file_source_alias_filename_insert_guard
BEFORE INSERT ON file_source_aliases
WHEN length(NEW.supplied_filename) > 260
  OR instr(NEW.supplied_filename, '/') <> 0
  OR instr(NEW.supplied_filename, '\\') <> 0
  OR instr(NEW.supplied_filename, char(0)) <> 0
  OR instr(NEW.supplied_filename, char(10)) <> 0
  OR instr(NEW.supplied_filename, char(13)) <> 0
  OR (substr(NEW.source_ref,1,20) = 'friday-message-name:'
      AND NEW.supplied_filename = '')
  OR (NEW.supplied_filename <> ''
      AND substr(NEW.source_ref,1,14) <> 'telegram-file:'
      AND NOT (length(NEW.source_ref) = 40
               AND substr(NEW.source_ref,1,24) = 'friday-message-name:msg_'
               AND substr(NEW.source_ref,25,16) NOT GLOB '*[^0-9a-f]*'))
  OR (NEW.supplied_filename <> ''
      AND length(NEW.source_ref) = 40
      AND substr(NEW.source_ref,1,24) = 'friday-message-name:msg_'
      AND (NOT EXISTS (
          SELECT 1 FROM messages m
          JOIN conversations c ON c.id=m.conversation_id AND c.user_id=m.user_id
          WHERE m.id=substr(NEW.source_ref,21,20)
            AND m.user_id=NEW.user_id AND m.role='user'
            AND m.content='Загружен документ: ' || NEW.supplied_filename
            AND json_valid(m.metadata_json)
            AND json_type(m.metadata_json,'$.synthetic_document_notice')='true'
            AND json_array_length(m.metadata_json,'$.conversation_attachment_raw_ids')=1
            AND json_array_length(m.metadata_json,'$.conversation_uploaded_raw_ids')=1
            AND json_extract(m.metadata_json,'$.conversation_attachment_raw_ids[0]')=NEW.raw_object_id
            AND json_extract(m.metadata_json,'$.conversation_uploaded_raw_ids[0]')=NEW.raw_object_id
      ) OR NOT EXISTS (
          SELECT 1 FROM raw_objects r
          JOIN users exact_alias_uploader
            ON exact_alias_uploader.id=NEW.uploaded_by
           AND exact_alias_uploader.status='active'
          WHERE r.id=NEW.raw_object_id AND r.user_id=NEW.user_id
            AND r.source='upload' AND r.content_type='file'
            AND r.deleted_at IS NULL
            AND CASE
              WHEN length(CAST(COALESCE(r.metadata_json,'') AS BLOB)) <= 131072
               AND typeof(r.metadata_json)='text'
               AND json_valid(r.metadata_json)
              THEN CASE
                WHEN json_type(r.metadata_json)='object'
                 AND NOT EXISTS (
                       SELECT 1 FROM json_tree(r.metadata_json) uploader_json_member
                        WHERE uploader_json_member.key IS NOT NULL
                        GROUP BY uploader_json_member.parent,
                                 CAST(uploader_json_member.key AS TEXT)
                       HAVING COUNT(*) > 1
                     )
                 AND json_type(r.metadata_json,'$.uploaded_by')='text'
                THEN json_extract(r.metadata_json,'$.uploaded_by')=NEW.uploaded_by
                ELSE 0
              END
              ELSE 0
            END
      )))
BEGIN
    SELECT RAISE(ABORT, 'invalid file source alias filename');
END;
CREATE TRIGGER IF NOT EXISTS file_source_alias_filename_update_guard
BEFORE UPDATE OF supplied_filename ON file_source_aliases
WHEN (OLD.supplied_filename <> '' AND NEW.supplied_filename <> OLD.supplied_filename)
  OR length(NEW.supplied_filename) > 260
  OR instr(NEW.supplied_filename, '/') <> 0
  OR instr(NEW.supplied_filename, '\\') <> 0
  OR instr(NEW.supplied_filename, char(0)) <> 0
  OR instr(NEW.supplied_filename, char(10)) <> 0
  OR instr(NEW.supplied_filename, char(13)) <> 0
  OR (substr(NEW.source_ref,1,20) = 'friday-message-name:'
      AND NEW.supplied_filename = '')
  OR (NEW.supplied_filename <> ''
      AND substr(NEW.source_ref,1,14) <> 'telegram-file:'
      AND NOT (length(NEW.source_ref) = 40
               AND substr(NEW.source_ref,1,24) = 'friday-message-name:msg_'
               AND substr(NEW.source_ref,25,16) NOT GLOB '*[^0-9a-f]*'))
  OR (NEW.supplied_filename <> ''
      AND length(NEW.source_ref) = 40
      AND substr(NEW.source_ref,1,24) = 'friday-message-name:msg_'
      AND (NOT EXISTS (
          SELECT 1 FROM messages m
          JOIN conversations c ON c.id=m.conversation_id AND c.user_id=m.user_id
          WHERE m.id=substr(NEW.source_ref,21,20)
            AND m.user_id=NEW.user_id AND m.role='user'
            AND m.content='Загружен документ: ' || NEW.supplied_filename
            AND json_valid(m.metadata_json)
            AND json_type(m.metadata_json,'$.synthetic_document_notice')='true'
            AND json_array_length(m.metadata_json,'$.conversation_attachment_raw_ids')=1
            AND json_array_length(m.metadata_json,'$.conversation_uploaded_raw_ids')=1
            AND json_extract(m.metadata_json,'$.conversation_attachment_raw_ids[0]')=NEW.raw_object_id
            AND json_extract(m.metadata_json,'$.conversation_uploaded_raw_ids[0]')=NEW.raw_object_id
      ) OR NOT EXISTS (
          SELECT 1 FROM raw_objects r
          JOIN users exact_alias_uploader
            ON exact_alias_uploader.id=NEW.uploaded_by
           AND exact_alias_uploader.status='active'
          WHERE r.id=NEW.raw_object_id AND r.user_id=NEW.user_id
            AND r.source='upload' AND r.content_type='file'
            AND r.deleted_at IS NULL
            AND CASE
              WHEN length(CAST(COALESCE(r.metadata_json,'') AS BLOB)) <= 131072
               AND typeof(r.metadata_json)='text'
               AND json_valid(r.metadata_json)
              THEN CASE
                WHEN json_type(r.metadata_json)='object'
                 AND NOT EXISTS (
                       SELECT 1 FROM json_tree(r.metadata_json) uploader_json_member
                        WHERE uploader_json_member.key IS NOT NULL
                        GROUP BY uploader_json_member.parent,
                                 CAST(uploader_json_member.key AS TEXT)
                       HAVING COUNT(*) > 1
                     )
                 AND json_type(r.metadata_json,'$.uploaded_by')='text'
                THEN json_extract(r.metadata_json,'$.uploaded_by')=NEW.uploaded_by
                ELSE 0
              END
              ELSE 0
            END
      )))
BEGIN
    SELECT RAISE(ABORT, 'immutable or invalid file source alias filename');
END;
CREATE TRIGGER IF NOT EXISTS file_source_alias_identity_update_guard
BEFORE UPDATE OF user_id, uploaded_by, source_ref, raw_object_id, created_at
ON file_source_aliases
WHEN NEW.user_id IS NOT OLD.user_id
  OR NEW.uploaded_by IS NOT OLD.uploaded_by
  OR NEW.source_ref IS NOT OLD.source_ref
  OR NEW.raw_object_id IS NOT OLD.raw_object_id
  OR NEW.created_at IS NOT OLD.created_at
BEGIN
    SELECT RAISE(ABORT, 'immutable file source alias identity');
END;
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
CREATE INDEX IF NOT EXISTS idx_knowledge_superseded_by
    ON knowledge_objects(superseded_by_id)
    WHERE superseded_by_id IS NOT NULL;
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
CREATE INDEX IF NOT EXISTS idx_inbox_knowledge
    ON inbox(knowledge_object_id)
    WHERE knowledge_object_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_inbox_user_action
    ON inbox(user_id, suggested_action, promotion_score DESC, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_entities_user_type ON entities(user_id, entity_type, normalized_name);
CREATE INDEX IF NOT EXISTS idx_links_entity ON knowledge_entity_links(user_id, entity_id, status);
CREATE INDEX IF NOT EXISTS idx_links_knowledge ON knowledge_entity_links(user_id, knowledge_object_id, status);
CREATE INDEX IF NOT EXISTS idx_relations_source ON relations(user_id, source_entity_id);
CREATE INDEX IF NOT EXISTS idx_relations_target ON relations(user_id, target_entity_id);
CREATE INDEX IF NOT EXISTS idx_entity_versions_recorded_at
    ON entity_versions(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_entity_merge_history_created_at
    ON entity_merge_history(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_entity_merge_history_undone_at
    ON entity_merge_history(undone_at DESC)
    WHERE undone_at IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_active_relation
    ON relations(user_id, source_entity_id, target_entity_id, relation_type)
    WHERE deleted_at IS NULL AND valid_to IS NULL;
CREATE INDEX IF NOT EXISTS idx_relation_revisions_user_time
    ON relation_revisions(user_id, recorded_at, relation_id, event_seq);
CREATE INDEX IF NOT EXISTS idx_relation_revisions_source_time
    ON relation_revisions(user_id, source_entity_id, recorded_at, relation_id, event_seq);
CREATE INDEX IF NOT EXISTS idx_relation_revisions_target_time
    ON relation_revisions(user_id, target_entity_id, recorded_at, relation_id, event_seq);

-- INSERT текущей проекции становится первой captured-версией. `strftime` —
-- fallback только для прямого SQL вне FridayStorage.transaction(); штатный путь
-- берёт единый микросекундный timestamp и batch из singleton context. SQLite
-- печатает миллисекунды, поэтому fallback сдвигает прошлую graph/history границу
-- минимум на 1 ms: простой MAX с равенством изменил бы уже выданный known_at.
CREATE TRIGGER IF NOT EXISTS relations_revision_ai
AFTER INSERT ON relations
BEGIN
    INSERT INTO relation_revisions(
        relation_id, revision, present, operation, recorded_at, batch_id,
        history_quality, user_id, source_entity_id, target_entity_id,
        relation_type, weight, metadata_json, created_at, deleted_at,
        valid_from, valid_to, invalidated_at, superseded_by
    ) VALUES(
        NEW.id,
        COALESCE((SELECT MAX(revision) + 1 FROM relation_revisions
                  WHERE relation_id=NEW.id), 1),
        1,
        'insert',
        COALESCE(
            NULLIF((SELECT recorded_at FROM relation_revision_context WHERE singleton=1), ''),
            MAX(
                strftime('%Y-%m-%dT%H:%M:%f000Z', 'now'),
                COALESCE((SELECT strftime('%Y-%m-%dT%H:%M:%f000Z', recorded_at, '+0.001 seconds')
                            FROM relation_revisions
                           ORDER BY event_seq DESC LIMIT 1), ''),
                COALESCE((SELECT strftime('%Y-%m-%dT%H:%M:%f000Z', created_at, '+0.001 seconds')
                            FROM entity_versions
                           ORDER BY created_at DESC, rowid DESC LIMIT 1), ''),
                COALESCE((SELECT strftime('%Y-%m-%dT%H:%M:%f000Z', created_at, '+0.001 seconds')
                            FROM entity_merge_history
                           ORDER BY created_at DESC, rowid DESC LIMIT 1), ''),
                COALESCE((SELECT strftime('%Y-%m-%dT%H:%M:%f000Z', undone_at, '+0.001 seconds')
                            FROM entity_merge_history
                           WHERE undone_at IS NOT NULL
                           ORDER BY undone_at DESC, rowid DESC LIMIT 1), ''),
                COALESCE((SELECT strftime('%Y-%m-%dT%H:%M:%f000Z', observed_at, '+0.001 seconds')
                            FROM relation_revision_context
                           WHERE singleton=1), ''),
                COALESCE((SELECT strftime('%Y-%m-%dT%H:%M:%f000Z', value, '+0.001 seconds')
                            FROM schema_meta
                           WHERE key='relation_history_complete_from'), '')
            )
        ),
        COALESCE(
            NULLIF((SELECT batch_id FROM relation_revision_context WHERE singleton=1), ''),
            'external:' || lower(hex(randomblob(16)))
        ),
        'captured',
        NEW.user_id, NEW.source_entity_id, NEW.target_entity_id,
        NEW.relation_type, NEW.weight, NEW.metadata_json, NEW.created_at,
        NEW.deleted_at, NEW.valid_from, NEW.valid_to, NEW.invalidated_at,
        NEW.superseded_by
    );
    UPDATE relation_revision_context
       SET observed_at=(SELECT recorded_at FROM relation_revisions
                         WHERE event_seq=last_insert_rowid())
     WHERE singleton=1
       AND observed_at < (SELECT recorded_at FROM relation_revisions
                           WHERE event_seq=last_insert_rowid());
END;

-- ID и tenant-владелец — идентичность append-only линии, а не редактируемое
-- содержимое. UPDATE id оставил бы OLD-линию present=1 без tombstone; UPDATE
-- user_id переписал бы владение всей прошлой связи сегодняшним tenant. Endpoints
-- здесь намеренно НЕ запрещены: merge/unmerge меняет их и capture хранит версии.
CREATE TRIGGER IF NOT EXISTS relations_revision_identity_immutable
BEFORE UPDATE OF id, user_id ON relations
WHEN OLD.id IS NOT NEW.id OR OLD.user_id IS NOT NEW.user_id
BEGIN
    SELECT RAISE(ABORT, 'relation id and user_id are immutable');
END;

-- REPLACE is not an UPDATE followed by our ordinary DELETE/INSERT capture.
-- With recursive_triggers=OFF (SQLite's default), its conflict deletion does not
-- run relations_revision_bd at all.  Refuse every INSERT that could displace a
-- current row, and keep a resurrected relation on its original tenant lineage.
-- A genuine unmerge remains legal: after a captured DELETE the current row is
-- absent, the latest revision is a tombstone, and the owner is unchanged.
CREATE TRIGGER IF NOT EXISTS relations_revision_insert_guard
BEFORE INSERT ON relations
BEGIN
    -- unmerge_entities uses INSERT OR IGNORE to restore the exact snapshot that
    -- a captured merge DELETE tombstoned.  If a later active row now owns that
    -- unique tuple, preserve OR IGNORE semantics without opening a REPLACE hole:
    -- RAISE(IGNORE) also turns a malicious OR REPLACE into a harmless no-op.
    SELECT RAISE(IGNORE)
    WHERE EXISTS (
        SELECT 1
        FROM relation_revisions AS tombstone
        WHERE tombstone.relation_id=NEW.id
          AND tombstone.revision=(
              SELECT MAX(latest.revision)
              FROM relation_revisions AS latest
              WHERE latest.relation_id=NEW.id
          )
          AND tombstone.present=0
          AND tombstone.user_id IS NEW.user_id
          AND tombstone.source_entity_id IS NEW.source_entity_id
          AND tombstone.target_entity_id IS NEW.target_entity_id
          AND tombstone.relation_type IS NEW.relation_type
          AND tombstone.weight IS NEW.weight
          AND tombstone.metadata_json IS NEW.metadata_json
          AND tombstone.created_at IS NEW.created_at
          AND tombstone.deleted_at IS NEW.deleted_at
          AND tombstone.valid_from IS NEW.valid_from
          AND tombstone.valid_to IS NEW.valid_to
          AND tombstone.invalidated_at IS NEW.invalidated_at
          AND tombstone.superseded_by IS NEW.superseded_by
    )
    AND EXISTS (
        SELECT 1
        FROM relations AS current
        WHERE NEW.deleted_at IS NULL
          AND NEW.valid_to IS NULL
          AND current.deleted_at IS NULL
          AND current.valid_to IS NULL
          AND current.user_id IS NEW.user_id
          AND current.source_entity_id IS NEW.source_entity_id
          AND current.target_entity_id IS NEW.target_entity_id
          AND current.relation_type IS NEW.relation_type
    );

    SELECT RAISE(ABORT, 'relation insert would replace current state or move identity')
    WHERE EXISTS (
        SELECT 1 FROM relations AS current WHERE current.id=NEW.id
     )
      OR EXISTS (
        SELECT 1
        FROM relations AS current
        WHERE NEW.deleted_at IS NULL
          AND NEW.valid_to IS NULL
          AND current.deleted_at IS NULL
          AND current.valid_to IS NULL
          AND current.user_id IS NEW.user_id
          AND current.source_entity_id IS NEW.source_entity_id
          AND current.target_entity_id IS NEW.target_entity_id
          AND current.relation_type IS NEW.relation_type
     )
      OR EXISTS (
        SELECT 1
        FROM relation_revisions AS history
        WHERE history.relation_id=NEW.id
          AND history.user_id IS NOT NEW.user_id
     )
      OR COALESCE((
        SELECT latest.present
        FROM relation_revisions AS latest
        WHERE latest.relation_id=NEW.id
        ORDER BY latest.revision DESC
        LIMIT 1
     ), 0)=1;
END;

-- UPDATE OR REPLACE can likewise delete the other row of the active partial
-- unique key without firing its DELETE capture trigger.  Check the would-be
-- projection before SQLite resolves the uniqueness conflict.
CREATE TRIGGER IF NOT EXISTS relations_revision_update_conflict_guard
BEFORE UPDATE OF user_id, source_entity_id, target_entity_id,
                 relation_type, deleted_at, valid_to ON relations
WHEN NEW.deleted_at IS NULL
 AND NEW.valid_to IS NULL
 AND EXISTS (
     SELECT 1
     FROM relations AS current
     WHERE current.id IS NOT OLD.id
       AND current.deleted_at IS NULL
       AND current.valid_to IS NULL
       AND current.user_id IS NEW.user_id
       AND current.source_entity_id IS NEW.source_entity_id
       AND current.target_entity_id IS NEW.target_entity_id
       AND current.relation_type IS NEW.relation_type
 )
BEGIN
    SELECT RAISE(ABORT, 'relation update would replace an active relation');
END;

-- No-op UPDATE не создаёт вымышленной версии. Перечень намеренно охватывает
-- КАЖДОЕ поле current projection: прямой SQL и будущий mutation path не могут
-- обойти историю только потому, что их ещё не знал Python-код.
CREATE TRIGGER IF NOT EXISTS relations_revision_au
AFTER UPDATE ON relations
WHEN OLD.id IS NOT NEW.id
  OR OLD.user_id IS NOT NEW.user_id
  OR OLD.source_entity_id IS NOT NEW.source_entity_id
  OR OLD.target_entity_id IS NOT NEW.target_entity_id
  OR OLD.relation_type IS NOT NEW.relation_type
  OR OLD.weight IS NOT NEW.weight
  OR OLD.metadata_json IS NOT NEW.metadata_json
  OR OLD.created_at IS NOT NEW.created_at
  OR OLD.deleted_at IS NOT NEW.deleted_at
  OR OLD.valid_from IS NOT NEW.valid_from
  OR OLD.valid_to IS NOT NEW.valid_to
  OR OLD.invalidated_at IS NOT NEW.invalidated_at
  OR OLD.superseded_by IS NOT NEW.superseded_by
BEGIN
    INSERT INTO relation_revisions(
        relation_id, revision, present, operation, recorded_at, batch_id,
        history_quality, user_id, source_entity_id, target_entity_id,
        relation_type, weight, metadata_json, created_at, deleted_at,
        valid_from, valid_to, invalidated_at, superseded_by
    ) VALUES(
        NEW.id,
        COALESCE((SELECT MAX(revision) + 1 FROM relation_revisions
                  WHERE relation_id=NEW.id), 1),
        1,
        'update',
        COALESCE(
            NULLIF((SELECT recorded_at FROM relation_revision_context WHERE singleton=1), ''),
            MAX(
                strftime('%Y-%m-%dT%H:%M:%f000Z', 'now'),
                COALESCE((SELECT strftime('%Y-%m-%dT%H:%M:%f000Z', recorded_at, '+0.001 seconds')
                            FROM relation_revisions
                           ORDER BY event_seq DESC LIMIT 1), ''),
                COALESCE((SELECT strftime('%Y-%m-%dT%H:%M:%f000Z', created_at, '+0.001 seconds')
                            FROM entity_versions
                           ORDER BY created_at DESC, rowid DESC LIMIT 1), ''),
                COALESCE((SELECT strftime('%Y-%m-%dT%H:%M:%f000Z', created_at, '+0.001 seconds')
                            FROM entity_merge_history
                           ORDER BY created_at DESC, rowid DESC LIMIT 1), ''),
                COALESCE((SELECT strftime('%Y-%m-%dT%H:%M:%f000Z', undone_at, '+0.001 seconds')
                            FROM entity_merge_history
                           WHERE undone_at IS NOT NULL
                           ORDER BY undone_at DESC, rowid DESC LIMIT 1), ''),
                COALESCE((SELECT strftime('%Y-%m-%dT%H:%M:%f000Z', observed_at, '+0.001 seconds')
                            FROM relation_revision_context
                           WHERE singleton=1), ''),
                COALESCE((SELECT strftime('%Y-%m-%dT%H:%M:%f000Z', value, '+0.001 seconds')
                            FROM schema_meta
                           WHERE key='relation_history_complete_from'), '')
            )
        ),
        COALESCE(
            NULLIF((SELECT batch_id FROM relation_revision_context WHERE singleton=1), ''),
            'external:' || lower(hex(randomblob(16)))
        ),
        'captured',
        NEW.user_id, NEW.source_entity_id, NEW.target_entity_id,
        NEW.relation_type, NEW.weight, NEW.metadata_json, NEW.created_at,
        NEW.deleted_at, NEW.valid_from, NEW.valid_to, NEW.invalidated_at,
        NEW.superseded_by
    );
    UPDATE relation_revision_context
       SET observed_at=(SELECT recorded_at FROM relation_revisions
                         WHERE event_seq=last_insert_rowid())
     WHERE singleton=1
       AND observed_at < (SELECT recorded_at FROM relation_revisions
                           WHERE event_seq=last_insert_rowid());
END;

-- DELETE оставляет полный OLD snapshot как present=0 tombstone. BEFORE нужен,
-- чтобы снимок гарантированно существовал до исчезновения current projection.
CREATE TRIGGER IF NOT EXISTS relations_revision_bd
BEFORE DELETE ON relations
BEGIN
    INSERT INTO relation_revisions(
        relation_id, revision, present, operation, recorded_at, batch_id,
        history_quality, user_id, source_entity_id, target_entity_id,
        relation_type, weight, metadata_json, created_at, deleted_at,
        valid_from, valid_to, invalidated_at, superseded_by
    ) VALUES(
        OLD.id,
        COALESCE((SELECT MAX(revision) + 1 FROM relation_revisions
                  WHERE relation_id=OLD.id), 1),
        0,
        'delete',
        COALESCE(
            NULLIF((SELECT recorded_at FROM relation_revision_context WHERE singleton=1), ''),
            MAX(
                strftime('%Y-%m-%dT%H:%M:%f000Z', 'now'),
                COALESCE((SELECT strftime('%Y-%m-%dT%H:%M:%f000Z', recorded_at, '+0.001 seconds')
                            FROM relation_revisions
                           ORDER BY event_seq DESC LIMIT 1), ''),
                COALESCE((SELECT strftime('%Y-%m-%dT%H:%M:%f000Z', created_at, '+0.001 seconds')
                            FROM entity_versions
                           ORDER BY created_at DESC, rowid DESC LIMIT 1), ''),
                COALESCE((SELECT strftime('%Y-%m-%dT%H:%M:%f000Z', created_at, '+0.001 seconds')
                            FROM entity_merge_history
                           ORDER BY created_at DESC, rowid DESC LIMIT 1), ''),
                COALESCE((SELECT strftime('%Y-%m-%dT%H:%M:%f000Z', undone_at, '+0.001 seconds')
                            FROM entity_merge_history
                           WHERE undone_at IS NOT NULL
                           ORDER BY undone_at DESC, rowid DESC LIMIT 1), ''),
                COALESCE((SELECT strftime('%Y-%m-%dT%H:%M:%f000Z', observed_at, '+0.001 seconds')
                            FROM relation_revision_context
                           WHERE singleton=1), ''),
                COALESCE((SELECT strftime('%Y-%m-%dT%H:%M:%f000Z', value, '+0.001 seconds')
                            FROM schema_meta
                           WHERE key='relation_history_complete_from'), '')
            )
        ),
        COALESCE(
            NULLIF((SELECT batch_id FROM relation_revision_context WHERE singleton=1), ''),
            'external:' || lower(hex(randomblob(16)))
        ),
        'captured',
        OLD.user_id, OLD.source_entity_id, OLD.target_entity_id,
        OLD.relation_type, OLD.weight, OLD.metadata_json, OLD.created_at,
        OLD.deleted_at, OLD.valid_from, OLD.valid_to, OLD.invalidated_at,
        OLD.superseded_by
    );
    UPDATE relation_revision_context
       SET observed_at=(SELECT recorded_at FROM relation_revisions
                         WHERE event_seq=last_insert_rowid())
     WHERE singleton=1
       AND observed_at < (SELECT recorded_at FROM relation_revisions
                           WHERE event_seq=last_insert_rowid());
END;

CREATE TRIGGER IF NOT EXISTS relation_revisions_append_only_update
BEFORE UPDATE ON relation_revisions
BEGIN
    SELECT RAISE(ABORT, 'relation revision history is append-only');
END;

CREATE TRIGGER IF NOT EXISTS relation_revisions_append_only_delete
BEFORE DELETE ON relation_revisions
BEGIN
    SELECT RAISE(ABORT, 'relation revision history is append-only');
END;

-- SQLite REPLACE технически является INSERT с последующим вытеснением строки и
-- при выключенном recursive_triggers способен обойти обычный DELETE guard. Явно
-- запрещаем конфликтующую вставку; новая уникальная revision от capture trigger
-- по-прежнему разрешена.
CREATE TRIGGER IF NOT EXISTS relation_revisions_append_only_replace
BEFORE INSERT ON relation_revisions
WHEN EXISTS (
        SELECT 1 FROM relation_revisions WHERE event_seq=NEW.event_seq
     )
  OR EXISTS (
        SELECT 1 FROM relation_revisions
        WHERE relation_id=NEW.relation_id AND revision=NEW.revision
     )
BEGIN
    SELECT RAISE(ABORT, 'relation revision history is append-only');
END;

-- observed_at — не кэш, а уже выданное читателю обещание. Оно может только
-- расти: отдельный historical read двигает его в пустом context, а managed
-- transaction атомарно ставит новый batch и тот же самый logical-clock instant.
CREATE TRIGGER IF NOT EXISTS relation_revision_context_monotonic_update
BEFORE UPDATE ON relation_revision_context
WHEN NEW.singleton IS NOT OLD.singleton
  OR NEW.observed_at < OLD.observed_at
  OR (
       NEW.observed_at IS NOT OLD.observed_at
       AND NOT (
           (OLD.batch_id='' AND OLD.recorded_at=''
            AND NEW.batch_id='' AND NEW.recorded_at='')
           OR (NEW.batch_id<>'' AND NEW.recorded_at=NEW.observed_at
               AND NEW.observed_at>OLD.observed_at)
       )
  )
BEGIN
    SELECT RAISE(ABORT, 'relation history observed boundary is immutable');
END;

CREATE TRIGGER IF NOT EXISTS relation_revision_context_immutable_delete
BEFORE DELETE ON relation_revision_context
BEGIN
    SELECT RAISE(ABORT, 'relation history observed boundary is immutable');
END;

-- Закрывает INSERT OR REPLACE при recursive_triggers=OFF. Первая строка
-- создаётся только миграцией до установки trigger; второй singleton не бывает.
CREATE TRIGGER IF NOT EXISTS relation_revision_context_singleton_insert
BEFORE INSERT ON relation_revision_context
WHEN EXISTS (SELECT 1 FROM relation_revision_context)
BEGIN
    SELECT RAISE(ABORT, 'relation history observed boundary is immutable');
END;

-- Исторический floor — обещание полноты, а не подвижная настройка. Разрешена
-- только первая INSERT миграцией; последующая правка или удаление fail-closed.
CREATE TRIGGER IF NOT EXISTS relation_history_floor_immutable_update
BEFORE UPDATE ON schema_meta
WHEN (OLD.key='relation_history_complete_from'
      AND (NEW.key IS NOT OLD.key OR NEW.value IS NOT OLD.value
           OR NEW.updated_at IS NOT OLD.updated_at))
  OR (OLD.key IS NOT 'relation_history_complete_from'
      AND NEW.key='relation_history_complete_from')
BEGIN
    SELECT RAISE(ABORT, 'relation history completeness floor is immutable');
END;

CREATE TRIGGER IF NOT EXISTS relation_history_floor_immutable_delete
BEFORE DELETE ON schema_meta
WHEN OLD.key='relation_history_complete_from'
BEGIN
    SELECT RAISE(ABORT, 'relation history completeness floor is immutable');
END;

-- То же закрывает INSERT OR REPLACE: SQLite не обещает вызвать DELETE trigger
-- вытесняемой строки без recursive_triggers, но BEFORE INSERT вызывается всегда.
CREATE TRIGGER IF NOT EXISTS relation_history_floor_immutable_insert
BEFORE INSERT ON schema_meta
WHEN NEW.key='relation_history_complete_from'
 AND EXISTS (
     SELECT 1 FROM schema_meta WHERE key='relation_history_complete_from'
 )
BEGIN
    SELECT RAISE(ABORT, 'relation history completeness floor is immutable');
END;
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
-- Dense passage reloads need newest objects first.  The chunk table cannot order
-- by its parent's created_at, so a dense, current corpus is walked parent-first;
-- id is the exact tie-breaker used by the public result order.
CREATE INDEX IF NOT EXISTS idx_knowledge_chunk_scan_order
    ON knowledge_objects(user_id, created_at DESC, id ASC)
    WHERE deleted_at IS NULL;
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
-- Дедуп по АДРЕСАТУ, а не по учётке: получает сообщение чат, а не строка в базе.
--
-- Найдено владельцем в живом Telegram 2026-08-04 («оповещение, правда два
-- подряд») и подтверждено по базе: у сторожа задвоены ВСЕ записи. Причина —
-- у владельца две учётки (`964e5f17…` через API и `telegram:telegram:467035772`
-- через бота), а чат один и тот же, 467035772. Ключ уникальности стоял по
-- `user_id`, поэтому обе строки проходили и в один чат приходило два одинаковых
-- сообщения.
--
-- Цена ошибки здесь выше обычной: этим каналом система сообщает о лежащей модели
-- и об утёкшем секрете, и удвоение превращает такое сообщение в шум.
CREATE UNIQUE INDEX IF NOT EXISTS uq_outbound_dedup
    ON outbound_notifications(chat_id, dedup_key) WHERE dedup_key <> '';
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

-- Сказанное в чате сказано навсегда. Требование владельца (2026-08-01): попало в
-- чат один раз — и всё, удалить нельзя. Защита стоит в САМОЙ базе, а не в коде
-- над ней: код можно обойти новым маршрутом, забытым скриптом или прямым SQL из
-- консоли, а триггер отменяет транзакцию в любом из этих случаев.
--
-- Правка содержимого — то же стирание, поэтому `content` и `role` тоже
-- неизменны. Остальные колонки (служебные метки, ссылки на разговор) менять
-- можно: они не были сказаны человеком.
CREATE TRIGGER IF NOT EXISTS messages_are_never_deleted BEFORE DELETE ON messages BEGIN
    SELECT RAISE(ABORT, 'сообщения чата неудаляемы: сказанное в чате остаётся навсегда');
END;

CREATE TRIGGER IF NOT EXISTS messages_are_never_rewritten BEFORE UPDATE OF content, role ON messages
BEGIN
    SELECT RAISE(ABORT, 'текст сообщения чата неизменяем: правка — то же стирание');
END;

-- Разговор — контейнер сообщений. Удалить его значило бы отрезать доступ к тому,
-- что удалять запрещено, поэтому он тоже остаётся; «удаление» разговора в API
-- переведено в архивирование.
CREATE TRIGGER IF NOT EXISTS conversations_are_never_deleted BEFORE DELETE ON conversations BEGIN
    SELECT RAISE(ABORT, 'разговоры неудаляемы: они держат историю чата');
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
    except OSError as exc:
        LOGGER.warning("Could not restrict private file permissions (%s)", type(exc).__name__)


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    ensure_private_directory(path.parent)
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

    ensure_private_directory(destination.parent)
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
    settings: FridaySettings,
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

    ensure_private_directory(settings.backups_dir)
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
                "Exact pre-restore SQLite files preserved for crash-safe rollback. "
                "This raw set is not a verified application backup; inspect manually "
                "before any out-of-band use."
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

    settings: FridaySettings
    execute: Callable[..., sqlite3.Cursor]
    transaction: Callable[..., Any]
    _observe_relation_history_boundary: Callable[[str], None]
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
    get_feedback_stats: Callable[..., dict[str, Any]]
    count_feedback_state: Callable[..., int]
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
    _engineer_command_backup_authority: Any | None
    _engineer_command_backup_authority_lock: Any
    _begin_database_restore_open: Callable[[], bool]
    _end_database_restore_open: Callable[[bool], None]
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
    "DATA_SOURCES_SCHEMA",
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
    "FridaySettings",
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
