"""Transactional kernel ledger. Isolated from production host-agent job tables."""

from __future__ import annotations

import json
import math
import os
import re
import secrets
import sqlite3
import stat
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Self

from friday.engineer_source_binding import (
    canonical_engineer_source_binding_sha256,
    canonical_engineer_source_step_id,
    legacy_engineer_source_binding_sha256,
)
from friday.user_ids import validate_user_id

from .contracts import CommandError, canonical_json_bytes, sha256_bytes
from .store_lifecycle import (
    CommandStoreLifecycle,
    CommandStoreOpenMode,
    command_store_backup_is_quiescent,
    validate_runtime_database,
)

_LOCK_EX = 2
_LOCK_NB = 4
_LOCK_UN = 8
_JOB_ID = re.compile(r"[0-9a-f]{32}")
_KNOWN_JOB_STATUSES = frozenset(
    {"planned", "admitted", "running", "completed", "failed", "cancelled", "timeout", "unknown"}
)
_UNRESOLVED_JOB_STATUSES = frozenset({"planned", "admitted", "running", "unknown"})
_CANCELLABLE_JOB_STATUSES = frozenset({"planned", "admitted", "running"})
_PUBLICATION_STATES = frozenset({"pending", "staged", "sent", "uncertain", "blocked"})
_PUBLICATION_RETRY_BASE_SEC = 5
_PUBLICATION_RETRY_MAX_SEC = 30 * 60
_PUBLICATION_ATTEMPTS_MAX = 2_147_483_647
_RETENTION_BATCH_MAX = 20
_EPHEMERAL_RETENTION_BATCH_MAX = 100
_PROGRESS_BATCH_MAX = 20
_PROGRESS_CHECKPOINTS = frozenset({0, 60, 300, 900, 1800})
_PROGRESS_RETRY_BASE_SEC = 30
_PROGRESS_RETRY_MAX_SEC = 30 * 60
_RETENTION_RETRY_BASE_SEC = 5 * 60
_RETENTION_RETRY_MAX_SEC = 7 * 24 * 60 * 60
_ENGINEER_WORK_ITEM_ID_RE = re.compile(r"ewi_[0-9a-f]{32}")
_ENGINEER_WORK_ITEM_IDEMPOTENCY_KEY_RE = re.compile(r"ecmd-[0-9a-f]{64}")
_ENGINEER_SOURCE_STEP_ID_RE = re.compile(r"ecstep-[0-9a-f]{32}")
_LOWER_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_ENGINEER_WORK_ITEM_MAX_REVISION = 2_147_483_647
_ENGINEER_WORK_ITEM_MAX_STEPS = 4_096
_HISTORICAL_SOURCE_STEP_SCHEMA = "friday.engineer-command-historical-source-step.v1"
_ACCOUNT_INVENTORY_SCHEMA = "friday.engineer-command-account-inventory.v1"


@dataclass(frozen=True, slots=True)
class EngineerCommandAccountInventory:
    """Content-free exact generation snapshot for permanent-account deletion."""

    user_id: str
    store_id: str
    authority_sequence: int
    jobs: int
    source_slots: int
    fences: int
    unattributable_fences: int
    publications: int
    output_jobs: int

    def __post_init__(self) -> None:
        self._validate()

    def _validate(self) -> None:
        try:
            canonical_user_id = validate_user_id(self.user_id)
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid Engineer command inventory user") from exc
        counts = (
            self.jobs,
            self.source_slots,
            self.fences,
            self.unattributable_fences,
            self.publications,
            self.output_jobs,
        )
        if (
            type(self.user_id) is not str
            or canonical_user_id != self.user_id
            or type(self.store_id) is not str
            or _JOB_ID.fullmatch(self.store_id) is None
            or type(self.authority_sequence) is not int
            or not 0 <= self.authority_sequence <= 9_223_372_036_854_775_806
            or any(type(value) is not int or value < 0 for value in counts)
        ):
            raise ValueError("invalid Engineer command inventory")

    @property
    def retained_roots(self) -> int:
        """Count immutable job/fence roots without double-counting projections."""

        return self.jobs + self.fences + self.unattributable_fences

    @property
    def has_history(self) -> bool:
        return any(
            value > 0
            for value in (
                self.jobs,
                self.source_slots,
                self.fences,
                self.unattributable_fences,
                self.publications,
                self.output_jobs,
            )
        )

    def fingerprint_payload(self) -> dict[str, str | int]:
        """Closed body-free projection safe to bind into the deletion plan."""

        self._validate()
        return {
            "schema": _ACCOUNT_INVENTORY_SCHEMA,
            "user_id": self.user_id,
            "store_id": self.store_id,
            "authority_sequence": self.authority_sequence,
            "jobs": self.jobs,
            "source_slots": self.source_slots,
            "fences": self.fences,
            "unattributable_fences": self.unattributable_fences,
            "publications": self.publications,
            "output_jobs": self.output_jobs,
        }


def _historical_source_step_id(
    *,
    job_id: str,
    actor_id: str,
    idempotency_key: str,
    command_digest: str,
) -> str:
    """Derive one stable v2 slot for a row created before source steps existed."""

    if (
        _JOB_ID.fullmatch(job_id) is None
        or not actor_id
        or len(actor_id) > 128
        or not idempotency_key
        or len(idempotency_key) > 128
        or _LOWER_SHA256_RE.fullmatch(command_digest) is None
    ):
        raise CommandError("engineer_command_source_slot_migration_invalid")
    digest = sha256_bytes(
        canonical_json_bytes(
            {
                "actor_id": actor_id,
                "command_digest": command_digest,
                "idempotency_key": idempotency_key,
                "job_id": job_id,
                "schema": _HISTORICAL_SOURCE_STEP_SCHEMA,
            }
        )
    )
    return canonical_engineer_source_step_id(f"ecstep-{digest[:32]}")


_ENGINEER_COMMAND_SOURCE_SLOT_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS engineer_command_source_slots (
    actor_id TEXT NOT NULL,
    source_binding_sha256 TEXT NOT NULL,
    legacy_source_binding_sha256 TEXT,
    idempotency_key TEXT NOT NULL,
    command_digest TEXT NOT NULL,
    target_kind TEXT NOT NULL,
    job_id TEXT,
    fence_actor_id TEXT,
    fence_idempotency_key TEXT,
    work_item_id TEXT,
    expected_revision INTEGER,
    step_ordinal INTEGER,
    created_at REAL NOT NULL,
    PRIMARY KEY(actor_id, source_binding_sha256),
    UNIQUE(actor_id, idempotency_key),
    UNIQUE(actor_id, legacy_source_binding_sha256),
    UNIQUE(job_id),
    UNIQUE(fence_actor_id, fence_idempotency_key),
    FOREIGN KEY(job_id) REFERENCES jobs(job_id)
        ON UPDATE RESTRICT ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED,
    FOREIGN KEY(fence_actor_id, fence_idempotency_key)
        REFERENCES engineer_work_item_idempotency_fences(actor_id, idempotency_key)
        ON UPDATE RESTRICT ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED,
    CHECK(typeof(actor_id)='text' AND length(actor_id) BETWEEN 1 AND 128
          AND actor_id=trim(actor_id) AND instr(actor_id, char(0))=0),
    CHECK(typeof(source_binding_sha256)='text' AND length(source_binding_sha256)=64
          AND source_binding_sha256 NOT GLOB '*[^0-9a-f]*'),
    CHECK(legacy_source_binding_sha256 IS NULL
          OR (typeof(legacy_source_binding_sha256)='text'
              AND length(legacy_source_binding_sha256)=64
              AND legacy_source_binding_sha256 NOT GLOB '*[^0-9a-f]*')),
    CHECK(typeof(idempotency_key)='text' AND length(idempotency_key) BETWEEN 1 AND 128
          AND idempotency_key=trim(idempotency_key) AND instr(idempotency_key, char(0))=0),
    CHECK(typeof(command_digest)='text' AND length(command_digest)=64
          AND command_digest NOT GLOB '*[^0-9a-f]*'),
    CHECK(target_kind IN ('job','engineer_work_item_fence')),
    CHECK(
        (target_kind='job'
         AND typeof(job_id)='text' AND length(job_id)=32
         AND job_id NOT GLOB '*[^0-9a-f]*'
         AND fence_actor_id IS NULL AND fence_idempotency_key IS NULL
         AND work_item_id IS NULL AND expected_revision IS NULL AND step_ordinal IS NULL)
        OR
        (target_kind='engineer_work_item_fence'
         AND job_id IS NULL
         AND fence_actor_id=actor_id AND fence_idempotency_key=idempotency_key
         AND typeof(work_item_id)='text' AND length(work_item_id)=36
         AND substr(work_item_id,1,4)='ewi_'
         AND substr(work_item_id,5) NOT GLOB '*[^0-9a-f]*'
         AND typeof(expected_revision)='integer'
         AND expected_revision>=1 AND expected_revision<2147483647
         AND typeof(step_ordinal)='integer' AND step_ordinal BETWEEN 1 AND 4096)
    ),
    CHECK(typeof(created_at)='real' AND created_at>=0 AND created_at<=253402300799)
) WITHOUT ROWID;
"""
_ENGINEER_COMMAND_SOURCE_SLOT_INSERT_AUTHORITY_SQL = """
CREATE TRIGGER IF NOT EXISTS trg_engineer_command_source_slot_insert_authority
BEFORE INSERT ON engineer_command_source_slots
WHEN friday_command_source_slot_authorized()<>1
BEGIN
    SELECT RAISE(ABORT, 'engineer_command_source_slot_unauthorized');
END;
"""
_ENGINEER_COMMAND_SOURCE_SLOT_CROSS_COLLISION_SQL = """
CREATE TRIGGER IF NOT EXISTS trg_engineer_command_source_slot_cross_collision
BEFORE INSERT ON engineer_command_source_slots
WHEN EXISTS (
    SELECT 1 FROM engineer_command_source_slots AS slot
     WHERE slot.actor_id=NEW.actor_id
       AND (
           slot.source_binding_sha256=NEW.legacy_source_binding_sha256
           OR slot.legacy_source_binding_sha256=NEW.source_binding_sha256
       )
)
BEGIN
    SELECT RAISE(ABORT, 'engineer_command_source_slot_collision');
END;
"""
_ENGINEER_COMMAND_SOURCE_SLOT_IMMUTABLE_UPDATE_SQL = """
CREATE TRIGGER IF NOT EXISTS trg_engineer_command_source_slot_immutable_update
BEFORE UPDATE ON engineer_command_source_slots
BEGIN
    SELECT RAISE(ABORT, 'engineer_command_source_slot_immutable');
END;
"""
_ENGINEER_COMMAND_SOURCE_SLOT_IMMUTABLE_DELETE_SQL = """
CREATE TRIGGER IF NOT EXISTS trg_engineer_command_source_slot_immutable_delete
BEFORE DELETE ON engineer_command_source_slots
BEGIN
    SELECT RAISE(ABORT, 'engineer_command_source_slot_immutable');
END;
"""
_ENGINEER_COMMAND_SOURCE_SLOT_JOB_INSERT_GUARD_SQL = """
CREATE TRIGGER IF NOT EXISTS trg_engineer_command_source_slot_job_insert_guard
BEFORE INSERT ON jobs
WHEN NOT EXISTS (
    SELECT 1 FROM engineer_command_source_slots AS slot
     WHERE slot.target_kind='job'
       AND slot.actor_id=NEW.actor_id
       AND slot.source_binding_sha256=NEW.source_binding_sha256
       AND slot.idempotency_key=NEW.idempotency_key
       AND slot.command_digest=NEW.command_digest
       AND slot.job_id=NEW.job_id
)
AND NOT EXISTS (
    SELECT 1 FROM jobs
     WHERE jobs.job_id=NEW.job_id
        OR (jobs.actor_id=NEW.actor_id AND jobs.idempotency_key=NEW.idempotency_key)
)
AND NOT EXISTS (
    SELECT 1 FROM engineer_work_item_idempotency_fences AS fence
     WHERE fence.actor_id=NEW.actor_id AND fence.idempotency_key=NEW.idempotency_key
)
BEGIN
    SELECT RAISE(ABORT, 'engineer_command_source_slot_missing');
END;
"""
_ENGINEER_COMMAND_SOURCE_SLOT_FENCE_INSERT_GUARD_SQL = """
CREATE TRIGGER IF NOT EXISTS trg_engineer_command_source_slot_fence_insert_guard
BEFORE INSERT ON engineer_work_item_idempotency_fences
WHEN typeof(NEW.expected_revision)='integer'
 AND typeof(NEW.step_ordinal)='integer'
 AND NOT EXISTS (
    SELECT 1 FROM engineer_command_source_slots AS slot
     WHERE slot.target_kind='engineer_work_item_fence'
       AND slot.actor_id=NEW.actor_id
       AND slot.source_binding_sha256=NEW.source_binding_sha256
       AND slot.idempotency_key=NEW.idempotency_key
       AND slot.command_digest=NEW.command_digest
       AND slot.fence_actor_id=NEW.actor_id
       AND slot.fence_idempotency_key=NEW.idempotency_key
       AND slot.work_item_id=NEW.work_item_id
       AND slot.expected_revision=NEW.expected_revision
       AND slot.step_ordinal=NEW.step_ordinal
)
AND NOT EXISTS (
    SELECT 1 FROM engineer_work_item_idempotency_fences AS fence
     WHERE (fence.actor_id=NEW.actor_id AND fence.idempotency_key=NEW.idempotency_key)
        OR (fence.actor_id=NEW.actor_id
            AND fence.work_item_id=NEW.work_item_id
            AND fence.expected_revision=NEW.expected_revision
            AND fence.step_ordinal=NEW.step_ordinal)
        OR (fence.actor_id=NEW.actor_id
            AND fence.source_binding_sha256=NEW.source_binding_sha256)
)
BEGIN
    SELECT RAISE(ABORT, 'engineer_command_source_slot_missing');
END;
"""
_ENGINEER_COMMAND_SOURCE_SLOT_SCHEMA = "\n".join(
    (
        _ENGINEER_COMMAND_SOURCE_SLOT_TABLE_SQL,
        _ENGINEER_COMMAND_SOURCE_SLOT_INSERT_AUTHORITY_SQL,
        _ENGINEER_COMMAND_SOURCE_SLOT_CROSS_COLLISION_SQL,
        _ENGINEER_COMMAND_SOURCE_SLOT_IMMUTABLE_UPDATE_SQL,
        _ENGINEER_COMMAND_SOURCE_SLOT_IMMUTABLE_DELETE_SQL,
        _ENGINEER_COMMAND_SOURCE_SLOT_JOB_INSERT_GUARD_SQL,
        _ENGINEER_COMMAND_SOURCE_SLOT_FENCE_INSERT_GUARD_SQL,
    )
)
_ENGINEER_COMMAND_SOURCE_SLOT_SCHEMA_OBJECTS = {
    "engineer_command_source_slots": (
        "table",
        "engineer_command_source_slots",
        _ENGINEER_COMMAND_SOURCE_SLOT_TABLE_SQL,
    ),
    "trg_engineer_command_source_slot_insert_authority": (
        "trigger",
        "engineer_command_source_slots",
        _ENGINEER_COMMAND_SOURCE_SLOT_INSERT_AUTHORITY_SQL,
    ),
    "trg_engineer_command_source_slot_cross_collision": (
        "trigger",
        "engineer_command_source_slots",
        _ENGINEER_COMMAND_SOURCE_SLOT_CROSS_COLLISION_SQL,
    ),
    "trg_engineer_command_source_slot_immutable_update": (
        "trigger",
        "engineer_command_source_slots",
        _ENGINEER_COMMAND_SOURCE_SLOT_IMMUTABLE_UPDATE_SQL,
    ),
    "trg_engineer_command_source_slot_immutable_delete": (
        "trigger",
        "engineer_command_source_slots",
        _ENGINEER_COMMAND_SOURCE_SLOT_IMMUTABLE_DELETE_SQL,
    ),
    "trg_engineer_command_source_slot_job_insert_guard": (
        "trigger",
        "jobs",
        _ENGINEER_COMMAND_SOURCE_SLOT_JOB_INSERT_GUARD_SQL,
    ),
    "trg_engineer_command_source_slot_fence_insert_guard": (
        "trigger",
        "engineer_work_item_idempotency_fences",
        _ENGINEER_COMMAND_SOURCE_SLOT_FENCE_INSERT_GUARD_SQL,
    ),
}

_ENGINEER_WORK_ITEM_FENCE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS engineer_work_item_idempotency_fences (
    actor_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    work_item_id TEXT NOT NULL,
    expected_revision INTEGER NOT NULL,
    step_ordinal INTEGER NOT NULL,
    source_binding_sha256 TEXT NOT NULL,
    command_digest TEXT NOT NULL,
    created_at REAL NOT NULL,
    PRIMARY KEY(actor_id, idempotency_key),
    UNIQUE(actor_id, work_item_id, expected_revision, step_ordinal),
    UNIQUE(actor_id, source_binding_sha256),
    CHECK(typeof(actor_id)='text' AND length(actor_id) BETWEEN 1 AND 128
          AND actor_id=trim(actor_id) AND instr(actor_id, char(0))=0),
    CHECK(typeof(idempotency_key)='text'
          AND length(idempotency_key)=69 AND substr(idempotency_key,1,5)='ecmd-'
          AND substr(idempotency_key,6) NOT GLOB '*[^0-9a-f]*'),
    CHECK(typeof(work_item_id)='text'
          AND length(work_item_id)=36 AND substr(work_item_id,1,4)='ewi_'
          AND substr(work_item_id,5) NOT GLOB '*[^0-9a-f]*'),
    CHECK(typeof(expected_revision)='integer'
          AND expected_revision>=1 AND expected_revision<2147483647),
    CHECK(typeof(step_ordinal)='integer' AND step_ordinal BETWEEN 1 AND 4096),
    CHECK(typeof(source_binding_sha256)='text' AND length(source_binding_sha256)=64
          AND source_binding_sha256 NOT GLOB '*[^0-9a-f]*'),
    CHECK(typeof(command_digest)='text' AND length(command_digest)=64
          AND command_digest NOT GLOB '*[^0-9a-f]*'),
    CHECK(typeof(created_at)='real' AND created_at>=0 AND created_at<=253402300799)
) WITHOUT ROWID;
"""
_ENGINEER_WORK_ITEM_FENCE_REJECT_JOB_SQL = """
CREATE TRIGGER IF NOT EXISTS trg_engineer_work_item_fence_reject_job
BEFORE INSERT ON engineer_work_item_idempotency_fences
WHEN EXISTS (
    SELECT 1 FROM jobs
     WHERE jobs.actor_id=NEW.actor_id
       AND jobs.idempotency_key=NEW.idempotency_key
)
BEGIN
    SELECT RAISE(ABORT, 'engineer_work_item_fence_job_exists');
END;
"""
_ENGINEER_WORK_ITEM_FENCE_COLLISION_GUARD_SQL = """
CREATE TRIGGER IF NOT EXISTS trg_engineer_work_item_fence_collision_guard
BEFORE INSERT ON engineer_work_item_idempotency_fences
WHEN EXISTS (
    SELECT 1 FROM engineer_work_item_idempotency_fences AS fence
     WHERE (fence.actor_id=NEW.actor_id
            AND fence.idempotency_key=NEW.idempotency_key)
        OR (fence.actor_id=NEW.actor_id
            AND fence.work_item_id=NEW.work_item_id
            AND fence.expected_revision=NEW.expected_revision
            AND fence.step_ordinal=NEW.step_ordinal)
        OR (fence.actor_id=NEW.actor_id
            AND fence.source_binding_sha256=NEW.source_binding_sha256)
)
BEGIN
    SELECT RAISE(ABORT, 'engineer_work_item_fence_collision');
END;
"""
_ENGINEER_WORK_ITEM_FENCE_IMMUTABLE_UPDATE_SQL = """
CREATE TRIGGER IF NOT EXISTS trg_engineer_work_item_fence_immutable_update
BEFORE UPDATE ON engineer_work_item_idempotency_fences
BEGIN
    SELECT RAISE(ABORT, 'engineer_work_item_fence_immutable');
END;
"""
_ENGINEER_WORK_ITEM_FENCE_IMMUTABLE_DELETE_SQL = """
CREATE TRIGGER IF NOT EXISTS trg_engineer_work_item_fence_immutable_delete
BEFORE DELETE ON engineer_work_item_idempotency_fences
BEGIN
    SELECT RAISE(ABORT, 'engineer_work_item_fence_immutable');
END;
"""
_ENGINEER_WORK_ITEM_FENCE_REJECT_INSERT_SQL = """
CREATE TRIGGER IF NOT EXISTS trg_engineer_work_item_fence_reject_job_insert
BEFORE INSERT ON jobs
WHEN EXISTS (
    SELECT 1 FROM engineer_work_item_idempotency_fences AS fence
     WHERE fence.actor_id=NEW.actor_id
       AND fence.idempotency_key=NEW.idempotency_key
)
BEGIN
    SELECT RAISE(ABORT, 'engineer_work_item_idempotency_fenced');
END;
"""
_ENGINEER_WORK_ITEM_FENCE_JOB_COLLISION_GUARD_SQL = """
CREATE TRIGGER IF NOT EXISTS trg_engineer_work_item_fence_job_collision_guard
BEFORE INSERT ON jobs
WHEN EXISTS (
    SELECT 1 FROM jobs
     WHERE jobs.job_id=NEW.job_id
        OR (jobs.actor_id=NEW.actor_id
            AND jobs.idempotency_key=NEW.idempotency_key)
)
BEGIN
    SELECT RAISE(ABORT, 'command_job_identity_collision');
END;
"""
_ENGINEER_WORK_ITEM_FENCE_JOB_IDENTITY_IMMUTABLE_SQL = """
CREATE TRIGGER IF NOT EXISTS trg_engineer_work_item_fence_job_identity_immutable
BEFORE UPDATE OF job_id, actor_id, tenant_id, conversation_id, channel,
                 source_row_id, source_step_id, source_binding_sha256,
                 source_hash, telegram_update_id,
                 idempotency_key, command_digest ON jobs
WHEN NEW.job_id IS NOT OLD.job_id
  OR NEW.actor_id IS NOT OLD.actor_id
  OR NEW.tenant_id IS NOT OLD.tenant_id
  OR NEW.conversation_id IS NOT OLD.conversation_id
  OR NEW.channel IS NOT OLD.channel
  OR NEW.source_row_id IS NOT OLD.source_row_id
  OR NEW.source_step_id IS NOT OLD.source_step_id
  OR NEW.source_binding_sha256 IS NOT OLD.source_binding_sha256
  OR NEW.source_hash IS NOT OLD.source_hash
  OR NEW.telegram_update_id IS NOT OLD.telegram_update_id
  OR NEW.idempotency_key IS NOT OLD.idempotency_key
  OR NEW.command_digest IS NOT OLD.command_digest
BEGIN
    SELECT RAISE(ABORT, 'command_job_identity_immutable');
END;
"""
_SOURCE_STEP_ONLY_ENGINEER_WORK_ITEM_FENCE_JOB_IDENTITY_IMMUTABLE_SQL = """
CREATE TRIGGER IF NOT EXISTS trg_engineer_work_item_fence_job_identity_immutable
BEFORE UPDATE OF job_id, actor_id, tenant_id, conversation_id, channel,
                 source_row_id, source_step_id, source_hash, telegram_update_id,
                 idempotency_key, command_digest ON jobs
WHEN NEW.job_id IS NOT OLD.job_id
  OR NEW.actor_id IS NOT OLD.actor_id
  OR NEW.tenant_id IS NOT OLD.tenant_id
  OR NEW.conversation_id IS NOT OLD.conversation_id
  OR NEW.channel IS NOT OLD.channel
  OR NEW.source_row_id IS NOT OLD.source_row_id
  OR NEW.source_step_id IS NOT OLD.source_step_id
  OR NEW.source_hash IS NOT OLD.source_hash
  OR NEW.telegram_update_id IS NOT OLD.telegram_update_id
  OR NEW.idempotency_key IS NOT OLD.idempotency_key
  OR NEW.command_digest IS NOT OLD.command_digest
BEGIN
    SELECT RAISE(ABORT, 'command_job_identity_immutable');
END;
"""
_LEGACY_ENGINEER_WORK_ITEM_FENCE_JOB_IDENTITY_IMMUTABLE_SQL = """
CREATE TRIGGER IF NOT EXISTS trg_engineer_work_item_fence_job_identity_immutable
BEFORE UPDATE OF job_id, actor_id, tenant_id, conversation_id, channel,
                 source_row_id, source_hash, telegram_update_id,
                 idempotency_key, command_digest ON jobs
WHEN NEW.job_id IS NOT OLD.job_id
  OR NEW.actor_id IS NOT OLD.actor_id
  OR NEW.tenant_id IS NOT OLD.tenant_id
  OR NEW.conversation_id IS NOT OLD.conversation_id
  OR NEW.channel IS NOT OLD.channel
  OR NEW.source_row_id IS NOT OLD.source_row_id
  OR NEW.source_hash IS NOT OLD.source_hash
  OR NEW.telegram_update_id IS NOT OLD.telegram_update_id
  OR NEW.idempotency_key IS NOT OLD.idempotency_key
  OR NEW.command_digest IS NOT OLD.command_digest
BEGIN
    SELECT RAISE(ABORT, 'command_job_identity_immutable');
END;
"""
_ENGINEER_WORK_ITEM_FENCE_JOB_DELETE_GUARD_SQL = """
CREATE TRIGGER IF NOT EXISTS trg_engineer_work_item_fence_job_delete_guard
BEFORE DELETE ON jobs
BEGIN
    SELECT RAISE(ABORT, 'command_job_immutable');
END;
"""
_ENGINEER_WORK_ITEM_FENCE_PUBLICATION_COLLISION_GUARD_SQL = """
CREATE TRIGGER IF NOT EXISTS trg_engineer_work_item_fence_publication_collision_guard
BEFORE INSERT ON command_job_publications
WHEN EXISTS (
    SELECT 1 FROM command_job_publications AS publication
     WHERE publication.job_id=NEW.job_id
)
BEGIN
    SELECT RAISE(ABORT, 'command_job_publication_identity_collision');
END;
"""
_ENGINEER_WORK_ITEM_FENCE_PUBLICATION_IDENTITY_IMMUTABLE_SQL = """
CREATE TRIGGER IF NOT EXISTS trg_engineer_work_item_fence_publication_identity_immutable
BEFORE UPDATE OF job_id, delivery_chat_id ON command_job_publications
WHEN NEW.job_id IS NOT OLD.job_id
  OR NEW.delivery_chat_id IS NOT OLD.delivery_chat_id
BEGIN
    SELECT RAISE(ABORT, 'command_job_publication_identity_immutable');
END;
"""
_ENGINEER_WORK_ITEM_FENCE_PUBLICATION_DELETE_GUARD_SQL = """
CREATE TRIGGER IF NOT EXISTS trg_engineer_work_item_fence_publication_delete_guard
BEFORE DELETE ON command_job_publications
BEGIN
    SELECT RAISE(ABORT, 'command_job_publication_immutable');
END;
"""
_ENGINEER_WORK_ITEM_FENCE_SCHEMA = "\n".join(
    (
        _ENGINEER_WORK_ITEM_FENCE_TABLE_SQL,
        _ENGINEER_WORK_ITEM_FENCE_REJECT_JOB_SQL,
        _ENGINEER_WORK_ITEM_FENCE_COLLISION_GUARD_SQL,
        _ENGINEER_WORK_ITEM_FENCE_IMMUTABLE_UPDATE_SQL,
        _ENGINEER_WORK_ITEM_FENCE_IMMUTABLE_DELETE_SQL,
        _ENGINEER_WORK_ITEM_FENCE_REJECT_INSERT_SQL,
        _ENGINEER_WORK_ITEM_FENCE_JOB_COLLISION_GUARD_SQL,
        _ENGINEER_WORK_ITEM_FENCE_JOB_IDENTITY_IMMUTABLE_SQL,
        _ENGINEER_WORK_ITEM_FENCE_JOB_DELETE_GUARD_SQL,
        _ENGINEER_WORK_ITEM_FENCE_PUBLICATION_COLLISION_GUARD_SQL,
        _ENGINEER_WORK_ITEM_FENCE_PUBLICATION_IDENTITY_IMMUTABLE_SQL,
        _ENGINEER_WORK_ITEM_FENCE_PUBLICATION_DELETE_GUARD_SQL,
    )
)
_ENGINEER_WORK_ITEM_FENCE_SCHEMA_OBJECTS = {
    "engineer_work_item_idempotency_fences": (
        "table",
        "engineer_work_item_idempotency_fences",
        _ENGINEER_WORK_ITEM_FENCE_TABLE_SQL,
    ),
    "trg_engineer_work_item_fence_reject_job": (
        "trigger",
        "engineer_work_item_idempotency_fences",
        _ENGINEER_WORK_ITEM_FENCE_REJECT_JOB_SQL,
    ),
    "trg_engineer_work_item_fence_collision_guard": (
        "trigger",
        "engineer_work_item_idempotency_fences",
        _ENGINEER_WORK_ITEM_FENCE_COLLISION_GUARD_SQL,
    ),
    "trg_engineer_work_item_fence_immutable_update": (
        "trigger",
        "engineer_work_item_idempotency_fences",
        _ENGINEER_WORK_ITEM_FENCE_IMMUTABLE_UPDATE_SQL,
    ),
    "trg_engineer_work_item_fence_immutable_delete": (
        "trigger",
        "engineer_work_item_idempotency_fences",
        _ENGINEER_WORK_ITEM_FENCE_IMMUTABLE_DELETE_SQL,
    ),
    "trg_engineer_work_item_fence_reject_job_insert": (
        "trigger",
        "jobs",
        _ENGINEER_WORK_ITEM_FENCE_REJECT_INSERT_SQL,
    ),
    "trg_engineer_work_item_fence_job_collision_guard": (
        "trigger",
        "jobs",
        _ENGINEER_WORK_ITEM_FENCE_JOB_COLLISION_GUARD_SQL,
    ),
    "trg_engineer_work_item_fence_job_identity_immutable": (
        "trigger",
        "jobs",
        _ENGINEER_WORK_ITEM_FENCE_JOB_IDENTITY_IMMUTABLE_SQL,
    ),
    "trg_engineer_work_item_fence_job_delete_guard": (
        "trigger",
        "jobs",
        _ENGINEER_WORK_ITEM_FENCE_JOB_DELETE_GUARD_SQL,
    ),
    "trg_engineer_work_item_fence_publication_collision_guard": (
        "trigger",
        "command_job_publications",
        _ENGINEER_WORK_ITEM_FENCE_PUBLICATION_COLLISION_GUARD_SQL,
    ),
    "trg_engineer_work_item_fence_publication_identity_immutable": (
        "trigger",
        "command_job_publications",
        _ENGINEER_WORK_ITEM_FENCE_PUBLICATION_IDENTITY_IMMUTABLE_SQL,
    ),
    "trg_engineer_work_item_fence_publication_delete_guard": (
        "trigger",
        "command_job_publications",
        _ENGINEER_WORK_ITEM_FENCE_PUBLICATION_DELETE_GUARD_SQL,
    ),
}


def _flock(fd: int, op: int) -> None:
    import fcntl

    fcntl.flock(fd, op)


def open_dir_nofollow(path: Path) -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        return os.open(str(path), flags)
    except OSError as exc:
        raise CommandError("workspace_unreadable") from exc


def atomic_write(path: Path, payload: bytes, *, mode: int = 0o600) -> None:
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)
    os.chmod(parent, 0o700)
    dir_fd = open_dir_nofollow(parent)
    tmp_name = f".{path.name}.{secrets.token_hex(8)}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = -1
    try:
        fd = os.open(tmp_name, flags, mode, dir_fd=dir_fd)
        os.fchmod(fd, mode)
        view = memoryview(payload)
        while view:
            written = os.write(fd, view)
            view = view[written:]
        os.fsync(fd)
        os.replace(tmp_name, path.name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        os.fsync(dir_fd)
    except OSError as exc:
        if fd >= 0:
            with suppress(OSError):
                os.unlink(tmp_name, dir_fd=dir_fd)
        raise CommandError("durable_write_failed") from exc
    finally:
        if fd >= 0:
            os.close(fd)
        os.close(dir_fd)


def atomic_write_json(path: Path, payload: dict[str, Any], *, mode: int = 0o600) -> None:
    atomic_write(path, canonical_json_bytes(payload) + b"\n", mode=mode)


def _canonical_schema_sql(value: str) -> str:
    without_if_not_exists = re.sub(
        r"\bIF\s+NOT\s+EXISTS\s+",
        "",
        str(value or ""),
        flags=re.IGNORECASE,
    )
    return " ".join(without_if_not_exists.rstrip().rstrip(";").split())


def _engineer_work_item_fence_identity(
    *,
    actor_id: object,
    idempotency_key: object,
    work_item_id: object,
    expected_revision: object,
    step_ordinal: object,
    source_binding_sha256: object,
    command_digest: object,
) -> tuple[str, str, str, int, int, str, str]:
    if (
        not isinstance(actor_id, str)
        or actor_id != actor_id.strip()
        or not 1 <= len(actor_id) <= 128
        or "\x00" in actor_id
    ):
        raise CommandError("idempotency_fence_scope_invalid")
    if (
        not isinstance(idempotency_key, str)
        or _ENGINEER_WORK_ITEM_IDEMPOTENCY_KEY_RE.fullmatch(idempotency_key) is None
    ):
        raise CommandError("idempotency_fence_identity_invalid")
    if not isinstance(work_item_id, str) or _ENGINEER_WORK_ITEM_ID_RE.fullmatch(work_item_id) is None:
        raise CommandError("idempotency_fence_identity_invalid")
    if (
        not isinstance(expected_revision, int)
        or isinstance(expected_revision, bool)
        or not 1 <= expected_revision < _ENGINEER_WORK_ITEM_MAX_REVISION
    ):
        raise CommandError("idempotency_fence_identity_invalid")
    if (
        not isinstance(step_ordinal, int)
        or isinstance(step_ordinal, bool)
        or not 1 <= step_ordinal <= _ENGINEER_WORK_ITEM_MAX_STEPS
    ):
        raise CommandError("idempotency_fence_identity_invalid")
    if (
        not isinstance(source_binding_sha256, str)
        or _LOWER_SHA256_RE.fullmatch(source_binding_sha256) is None
        or not isinstance(command_digest, str)
        or _LOWER_SHA256_RE.fullmatch(command_digest) is None
    ):
        raise CommandError("idempotency_fence_identity_invalid")
    return (
        actor_id,
        idempotency_key,
        work_item_id,
        expected_revision,
        step_ordinal,
        source_binding_sha256,
        command_digest,
    )


def _engineer_work_item_fence_key(actor_id: object, idempotency_key: object) -> tuple[str, str]:
    if not isinstance(actor_id, str) or not 1 <= len(actor_id) <= 128 or "\x00" in actor_id:
        raise CommandError("idempotency_fence_scope_invalid")
    if (
        not isinstance(idempotency_key, str)
        or not 1 <= len(idempotency_key) <= 128
        or "\x00" in idempotency_key
    ):
        raise CommandError("idempotency_fence_identity_invalid")
    return actor_id, idempotency_key


def _engineer_work_item_fence_source(
    actor_id: object,
    source_binding_sha256: object,
) -> tuple[str, str]:
    if (
        not isinstance(actor_id, str)
        or actor_id != actor_id.strip()
        or not 1 <= len(actor_id) <= 128
        or "\x00" in actor_id
    ):
        raise CommandError("idempotency_fence_scope_invalid")
    if (
        not isinstance(source_binding_sha256, str)
        or _LOWER_SHA256_RE.fullmatch(source_binding_sha256) is None
    ):
        raise CommandError("idempotency_fence_identity_invalid")
    return actor_id, source_binding_sha256


def _engineer_work_item_fence_projection(row: sqlite3.Row) -> dict[str, str | int]:
    try:
        actor_id = row["actor_id"]
        work_item_id = row["work_item_id"]
        expected_revision = row["expected_revision"]
        step_ordinal = row["step_ordinal"]
        source_binding_sha256 = row["source_binding_sha256"]
        idempotency_key = row["idempotency_key"]
        command_digest = row["command_digest"]
        if (
            type(actor_id) is not str
            or type(work_item_id) is not str
            or type(expected_revision) is not int
            or type(step_ordinal) is not int
            or type(source_binding_sha256) is not str
            or type(idempotency_key) is not str
            or type(command_digest) is not str
        ):
            raise TypeError("invalid fence projection type")
        projection: dict[str, str | int] = {
            "actor_id": actor_id,
            "work_item_id": work_item_id,
            "expected_revision": expected_revision,
            "step_ordinal": step_ordinal,
            "source_binding_sha256": source_binding_sha256,
            "idempotency_key": idempotency_key,
            "command_digest": command_digest,
        }
        _engineer_work_item_fence_identity(**projection)
        created_at = row["created_at"]
        if (
            type(created_at) is not float
            or not math.isfinite(created_at)
            or not 0 <= created_at <= 253_402_300_799
        ):
            raise ValueError("invalid fence timestamp")
    except (CommandError, KeyError, TypeError, ValueError, OverflowError) as exc:
        raise CommandError("idempotency_fence_corrupt") from exc
    return projection


def _engineer_command_source_slot_projection(
    row: sqlite3.Row,
) -> dict[str, str | int | None]:
    try:
        actor_id = row["actor_id"]
        source_binding = row["source_binding_sha256"]
        legacy_source_binding = row["legacy_source_binding_sha256"]
        idempotency_key = row["idempotency_key"]
        command_digest = row["command_digest"]
        target_kind = row["target_kind"]
        job_id = row["job_id"]
        fence_actor_id = row["fence_actor_id"]
        fence_idempotency_key = row["fence_idempotency_key"]
        work_item_id = row["work_item_id"]
        expected_revision = row["expected_revision"]
        step_ordinal = row["step_ordinal"]
        created_at = row["created_at"]
        if (
            type(actor_id) is not str
            or actor_id != actor_id.strip()
            or not 1 <= len(actor_id) <= 128
            or "\x00" in actor_id
            or type(source_binding) is not str
            or _LOWER_SHA256_RE.fullmatch(source_binding) is None
            or (
                legacy_source_binding is not None
                and (
                    type(legacy_source_binding) is not str
                    or _LOWER_SHA256_RE.fullmatch(legacy_source_binding) is None
                )
            )
            or type(idempotency_key) is not str
            or idempotency_key != idempotency_key.strip()
            or not 1 <= len(idempotency_key) <= 128
            or "\x00" in idempotency_key
            or type(command_digest) is not str
            or _LOWER_SHA256_RE.fullmatch(command_digest) is None
            or type(created_at) is not float
            or not math.isfinite(created_at)
            or not 0 <= created_at <= 253_402_300_799
        ):
            raise ValueError("invalid source slot")
        if target_kind == "job":
            if (
                type(job_id) is not str
                or _JOB_ID.fullmatch(job_id) is None
                or any(
                    value is not None
                    for value in (
                        fence_actor_id,
                        fence_idempotency_key,
                        work_item_id,
                        expected_revision,
                        step_ordinal,
                    )
                )
            ):
                raise ValueError("invalid job source slot")
        elif target_kind == "engineer_work_item_fence":
            if (
                job_id is not None
                or fence_actor_id != actor_id
                or fence_idempotency_key != idempotency_key
                or type(work_item_id) is not str
                or _ENGINEER_WORK_ITEM_ID_RE.fullmatch(work_item_id) is None
                or type(expected_revision) is not int
                or not 1 <= expected_revision < _ENGINEER_WORK_ITEM_MAX_REVISION
                or type(step_ordinal) is not int
                or not 1 <= step_ordinal <= _ENGINEER_WORK_ITEM_MAX_STEPS
            ):
                raise ValueError("invalid fence source slot")
        else:
            raise ValueError("invalid source slot target")
    except (IndexError, KeyError, TypeError, ValueError, OverflowError) as exc:
        raise CommandError("engineer_command_source_slot_corrupt") from exc
    return {
        "actor_id": actor_id,
        "source_binding_sha256": source_binding,
        "legacy_source_binding_sha256": legacy_source_binding,
        "idempotency_key": idempotency_key,
        "command_digest": command_digest,
        "target_kind": target_kind,
        "job_id": job_id,
        "fence_actor_id": fence_actor_id,
        "fence_idempotency_key": fence_idempotency_key,
        "work_item_id": work_item_id,
        "expected_revision": expected_revision,
        "step_ordinal": step_ordinal,
    }


def validate_command_store_runtime_schema(connection: sqlite3.Connection) -> None:
    """Run the complete immutable command-ledger schema proof read-only.

    Backup observers cannot construct a second ``CommandJobStore`` because its
    process lease is intentionally singleton.  Keeping this proof on an
    already-open read-only connection lets both runtime open and online backup
    authenticate the exact same tables, triggers, projections, and cross-row
    invariants without a competing kernel or a storage import cycle.
    """

    CommandJobStore._validate_runtime_schema_connection(connection)


class CommandJobStore:
    """SQLite authority ledger plus per-job workspace directories."""

    def __init__(
        self,
        root: Path,
        *,
        lifecycle_mode: CommandStoreOpenMode = "provision",
        lifecycle_key: bytes | None = None,
        lifecycle_state_dir: Path | None = None,
    ) -> None:
        if lifecycle_mode not in {"provision", "runtime"}:
            raise CommandError("command_store_open_mode_invalid")
        self._strict_runtime = lifecycle_mode == "runtime"
        self.root = Path(root)
        try:
            if lifecycle_mode == "provision":
                if self.root.is_symlink():
                    raise CommandError("command_store_state_dir_invalid")
                self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            root_stat = self.root.lstat()
            if (
                not stat.S_ISDIR(root_stat.st_mode)
                or stat.S_ISLNK(root_stat.st_mode)
                or root_stat.st_uid != os.geteuid()
                or (lifecycle_mode == "runtime" and root_stat.st_mode & 0o077)
            ):
                raise CommandError("command_store_state_dir_invalid")
            if lifecycle_mode == "provision":
                os.chmod(self.root, 0o700, follow_symlinks=False)
        except CommandError:
            raise
        except OSError as exc:
            raise CommandError("command_store_state_dir_invalid") from exc
        self._jobs = self.root / "jobs"
        self._workbenches = self.root / "workbenches"
        for private_dir in (self._jobs, self._workbenches):
            try:
                if lifecycle_mode == "provision":
                    if private_dir.is_symlink():
                        raise CommandError("durable_write_failed")
                    private_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
                    os.chmod(private_dir, 0o700, follow_symlinks=False)
                private_stat = private_dir.lstat()
                if (
                    not stat.S_ISDIR(private_stat.st_mode)
                    or stat.S_ISLNK(private_stat.st_mode)
                    or private_stat.st_uid != os.geteuid()
                    or (lifecycle_mode == "runtime" and private_stat.st_mode & 0o077)
                ):
                    raise CommandError("durable_write_failed")
            except CommandError:
                raise
            except OSError as exc:
                raise CommandError("durable_write_failed") from exc
        self.db_path = self.root / "kernel.sqlite"
        self._lifecycle = CommandStoreLifecycle(
            database_path=self.db_path,
            state_dir=self.root if lifecycle_state_dir is None else Path(lifecycle_state_dir),
            mode=lifecycle_mode,
            key=lifecycle_key,
        )
        if lifecycle_mode == "runtime":
            self._lifecycle.preflight_runtime_database()
        lease_path = self.root / "kernel.lease"
        if self.db_path.is_symlink() or (self.root / "kernel.lock").is_symlink() or lease_path.is_symlink():
            raise CommandError("durable_write_failed")
        lock_path = self.root / "kernel.lock"
        lock_flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            self._lock_fd = os.open(str(lock_path), lock_flags, 0o600)
        except OSError as exc:
            raise CommandError("durable_write_failed") from exc
        os.fchmod(self._lock_fd, 0o600)
        try:
            self._lease_fd = os.open(str(lease_path), lock_flags, 0o600)
            os.fchmod(self._lease_fd, 0o600)
        except OSError as exc:
            with suppress(OSError):
                os.close(getattr(self, "_lease_fd", -1))
            os.close(self._lock_fd)
            raise CommandError("durable_write_failed") from exc
        try:
            _flock(self._lease_fd, _LOCK_EX | _LOCK_NB)
        except OSError as exc:
            os.close(self._lease_fd)
            os.close(self._lock_fd)
            raise CommandError("command_kernel_already_active") from exc
        self._local = threading.RLock()
        self._closed = False
        self._source_slot_authority_depth = 0
        self.fail_next_commit = 0
        try:
            database_target = str(self.db_path)
            database_uri = False
            if lifecycle_mode == "runtime":
                database_target = f"{self.db_path.resolve().as_uri()}?mode=rw"
                database_uri = True
            self._conn = sqlite3.connect(
                database_target,
                isolation_level=None,
                check_same_thread=False,
                uri=database_uri,
            )
            self._conn.row_factory = sqlite3.Row
            self._conn.create_function(
                "friday_command_source_slot_authorized",
                0,
                lambda: 1 if self._source_slot_authority_depth > 0 else 0,
            )
            if lifecycle_mode == "provision":
                legacy = self._lifecycle.preflight_provision(self._conn)
                self._conn.execute("PRAGMA journal_mode=WAL")
                self._conn.execute("PRAGMA synchronous=FULL")
                self._conn.execute("PRAGMA foreign_keys=ON")
                self._init_schema()
                if lifecycle_key is not None:
                    validate_runtime_database(self._conn)
                    self._validate_runtime_schema()
                self._lifecycle.finish_provision(self._conn, legacy=legacy)
            else:
                validate_runtime_database(self._conn)
                if str(self._conn.execute("PRAGMA journal_mode").fetchone()[0]).lower() != "wal":
                    raise CommandError("command_store_database_invalid")
                self._conn.execute("PRAGMA synchronous=FULL")
                self._conn.execute("PRAGMA foreign_keys=ON")
                self._lifecycle.open_runtime(self._conn)
                self._validate_runtime_schema()
                self._lifecycle.assert_ready(self._conn)
        except Exception:
            with suppress(Exception):
                self._conn.close()
            os.close(self._lease_fd)
            os.close(self._lock_fd)
            raise

    @classmethod
    def provision(
        cls,
        root: Path,
        *,
        lifecycle_key: bytes | None = None,
        lifecycle_state_dir: Path | None = None,
    ) -> Self:
        """Explicitly create/upgrade a store; the keyless form is test-safe only."""

        return cls(
            root,
            lifecycle_mode="provision",
            lifecycle_key=lifecycle_key,
            lifecycle_state_dir=lifecycle_state_dir,
        )

    @classmethod
    def open_runtime(
        cls,
        root: Path,
        *,
        lifecycle_key: bytes,
        lifecycle_state_dir: Path | None = None,
    ) -> Self:
        """Open an exactly provisioned authority ledger without creating or healing it."""

        return cls(
            root,
            lifecycle_mode="runtime",
            lifecycle_key=lifecycle_key,
            lifecycle_state_dir=lifecycle_state_dir,
        )

    def close(self) -> None:
        with self._local:
            if self._closed:
                return
            self._closed = True
            try:
                self._conn.close()
            finally:
                os.close(self._lease_fd)
                os.close(self._lock_fd)

    def assert_lifecycle_ready(self) -> None:
        """Fail closed immediately before an external effect such as spawning."""

        with self._local:
            _flock(self._lock_fd, _LOCK_EX)
            try:
                self._lifecycle.assert_ready(self._conn)
            finally:
                _flock(self._lock_fd, _LOCK_UN)

    def backup_authority_snapshot(self) -> tuple[str, int, bool]:
        """Return the exact authenticated ledger generation for backup fencing."""

        with self._local:
            _flock(self._lock_fd, _LOCK_EX)
            try:
                store_id, sequence = self._lifecycle.authenticated_identity(self._conn)
                return store_id, sequence, command_store_backup_is_quiescent(self._conn)
            finally:
                _flock(self._lock_fd, _LOCK_UN)

    def attest_main_database_backup(
        self,
        database_sha256: str,
    ) -> dict[str, str | int | bool]:
        """Authenticate one copied main-database image with ledger authority."""

        with self._local:
            _flock(self._lock_fd, _LOCK_EX)
            try:
                return self._lifecycle.attest_main_database_backup(
                    self._conn,
                    database_sha256=database_sha256,
                )
            finally:
                _flock(self._lock_fd, _LOCK_UN)

    def verify_main_database_backup_authority(
        self,
        evidence: object,
        database_sha256: str,
    ) -> tuple[str, int, bool]:
        """Require a backup proof to match the still-current ledger generation."""

        with self._local:
            _flock(self._lock_fd, _LOCK_EX)
            try:
                store_id, sequence = self._lifecycle.verify_main_database_backup_authority(
                    self._conn,
                    evidence,
                    database_sha256=database_sha256,
                )
                return store_id, sequence, True
            finally:
                _flock(self._lock_fd, _LOCK_UN)

    def account_deletion_inventory(self, user_id: str) -> EngineerCommandAccountInventory:
        """Inventory one account under the authenticated exclusive ledger lease.

        Jobs are selected by actor *or tenant*: a delegated actor can leave
        target-owned output while having a different actor id.  Source slots and
        publications are counted explicitly for the deletion fingerprint even
        though their validated foreign-key/invariant closure makes every one of
        them subordinate to a retained job or fence root.
        """

        try:
            subject = validate_user_id(user_id)
        except ValueError as exc:
            raise CommandError("command_store_account_inventory_scope_invalid") from exc
        with self._local:
            _flock(self._lock_fd, _LOCK_EX)
            try:
                store_id, authority_sequence = self._lifecycle.authenticated_identity(self._conn)
                row = self._conn.execute(
                    """SELECT
                           (SELECT COUNT(*) FROM jobs
                             WHERE actor_id=:user_id OR tenant_id=:user_id) AS jobs,
                           (SELECT COUNT(*) FROM engineer_command_source_slots
                             WHERE actor_id=:user_id) AS source_slots,
                           (SELECT COUNT(*) FROM engineer_work_item_idempotency_fences
                             WHERE actor_id=:user_id) AS fences,
                           (SELECT COUNT(*) FROM engineer_work_item_idempotency_fences
                             WHERE actor_id<>:user_id) AS unattributable_fences,
                           (SELECT COUNT(*)
                              FROM command_job_publications publication
                              JOIN jobs ON jobs.job_id=publication.job_id
                             WHERE jobs.actor_id=:user_id
                                OR jobs.tenant_id=:user_id) AS publications,
                           (SELECT COUNT(*) FROM jobs
                             WHERE (actor_id=:user_id OR tenant_id=:user_id)
                               AND (receipt_mac IS NOT NULL
                                    OR stdout_sha256 IS NOT NULL
                                    OR stderr_sha256 IS NOT NULL
                                    OR generated_files_json IS NOT NULL
                                    OR stdout_bytes<>0 OR stderr_bytes<>0
                                    OR workspace_retired_at IS NOT NULL)) AS output_jobs""",
                    {"user_id": subject},
                ).fetchone()
                if row is None:
                    raise CommandError("command_store_account_inventory_unavailable")
                counts = {
                    name: row[name]
                    for name in (
                        "jobs",
                        "source_slots",
                        "fences",
                        "unattributable_fences",
                        "publications",
                        "output_jobs",
                    )
                }
                if any(type(value) is not int or value < 0 for value in counts.values()):
                    raise CommandError("command_store_account_inventory_invalid")
                return EngineerCommandAccountInventory(
                    user_id=subject,
                    store_id=store_id,
                    authority_sequence=authority_sequence,
                    jobs=counts["jobs"],
                    source_slots=counts["source_slots"],
                    fences=counts["fences"],
                    unattributable_fences=counts["unattributable_fences"],
                    publications=counts["publications"],
                    output_jobs=counts["output_jobs"],
                )
            except CommandError:
                raise
            except sqlite3.DatabaseError as exc:
                raise CommandError("command_store_account_inventory_unavailable") from exc
            finally:
                _flock(self._lock_fd, _LOCK_UN)

    @contextmanager
    def _source_slot_authority(self) -> Iterator[None]:
        self._source_slot_authority_depth += 1
        try:
            yield
        finally:
            self._source_slot_authority_depth -= 1

    def workbench_dir(
        self,
        *,
        actor_id: str,
        tenant_id: str,
        conversation_id: str,
    ) -> Path:
        """Return one durable owner/conversation workbench without exposing identifiers."""

        identities = (actor_id, tenant_id, conversation_id)
        if any(
            type(value) is not str or not value or len(value) > 128 or "\x00" in value for value in identities
        ):
            raise CommandError("workbench_scope_invalid")
        scope = {
            "actor_id": actor_id,
            "conversation_id": conversation_id,
            "schema": "friday.engineer.host-workbench.v1",
            "tenant_id": tenant_id,
        }
        workbench = self._workbenches / sha256_bytes(canonical_json_bytes(scope))
        try:
            workbench.mkdir(mode=0o700, exist_ok=True)
            observed = workbench.lstat()
            if (
                not stat.S_ISDIR(observed.st_mode)
                or stat.S_ISLNK(observed.st_mode)
                or observed.st_uid != os.geteuid()
            ):
                raise CommandError("workbench_scope_unavailable")
            os.chmod(workbench, 0o700, follow_symlinks=False)
        except CommandError:
            raise
        except OSError as exc:
            raise CommandError("workbench_scope_unavailable") from exc
        return workbench

    def _init_schema(self) -> None:
        self._upgrade_source_step_schema_for_provision()
        self._preflight_engineer_work_item_fence_schema()
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS jobs (
                job_id TEXT PRIMARY KEY,
                actor_id TEXT NOT NULL,
                tenant_id TEXT NOT NULL,
                conversation_id TEXT NOT NULL,
                channel TEXT NOT NULL,
                source_row_id TEXT NOT NULL,
                source_step_id TEXT NOT NULL,
                source_binding_sha256 TEXT NOT NULL,
                source_hash TEXT NOT NULL,
                telegram_update_id TEXT NOT NULL,
                isolation_profile TEXT NOT NULL,
                host_user_authorized INTEGER NOT NULL,
                idempotency_key TEXT NOT NULL,
                command_digest TEXT NOT NULL,
                input_manifest_sha256 TEXT NOT NULL DEFAULT '',
                argv_sha256 TEXT NOT NULL,
                lane TEXT NOT NULL,
                origin TEXT NOT NULL,
                status TEXT NOT NULL,
                error_code TEXT NOT NULL DEFAULT '',
                pid INTEGER,
                pid_starttime INTEGER,
                cgroup_path TEXT,
                systemd_unit TEXT,
                grant_nonce TEXT NOT NULL,
                timeout_sec INTEGER NOT NULL,
                max_stdout_bytes INTEGER NOT NULL,
                max_stderr_bytes INTEGER NOT NULL,
                effect_boundary_crossed INTEGER NOT NULL DEFAULT 0,
                cleanup_pending INTEGER NOT NULL DEFAULT 0,
                cancel_requested_at REAL,
                cancelled INTEGER NOT NULL DEFAULT 0,
                timed_out INTEGER NOT NULL DEFAULT 0,
                truncated_stdout INTEGER NOT NULL DEFAULT 0,
                truncated_stderr INTEGER NOT NULL DEFAULT 0,
                exit_code INTEGER,
                signal INTEGER,
                started_at REAL,
                finished_at REAL,
                stdout_sha256 TEXT,
                stderr_sha256 TEXT,
                stdout_bytes INTEGER NOT NULL DEFAULT 0,
                stderr_bytes INTEGER NOT NULL DEFAULT 0,
                generated_files_json TEXT,
                executable_json TEXT,
                receipt_mac TEXT,
                workspace_retired_at REAL,
                created_at REAL NOT NULL,
                UNIQUE(actor_id, idempotency_key)
            );
            CREATE TABLE IF NOT EXISTS command_job_focus (
                actor_id TEXT NOT NULL,
                tenant_id TEXT NOT NULL,
                conversation_id TEXT NOT NULL,
                channel TEXT NOT NULL,
                job_id TEXT NOT NULL UNIQUE REFERENCES jobs(job_id) ON DELETE RESTRICT,
                revision INTEGER NOT NULL CHECK(revision >= 1),
                focused_at REAL NOT NULL,
                focus_reason TEXT NOT NULL,
                PRIMARY KEY(actor_id, tenant_id, conversation_id, channel)
            );
            CREATE TRIGGER IF NOT EXISTS command_job_focus_scope_insert
            BEFORE INSERT ON command_job_focus
            WHEN NOT EXISTS (
                SELECT 1 FROM jobs
                 WHERE jobs.job_id=NEW.job_id
                   AND jobs.actor_id=NEW.actor_id
                   AND jobs.tenant_id=NEW.tenant_id
                   AND jobs.conversation_id=NEW.conversation_id
                   AND jobs.channel=NEW.channel
            )
            BEGIN
                SELECT RAISE(ABORT, 'command_job_focus_scope_mismatch');
            END;
            CREATE TRIGGER IF NOT EXISTS command_job_focus_scope_update
            BEFORE UPDATE ON command_job_focus
            WHEN NOT EXISTS (
                SELECT 1 FROM jobs
                 WHERE jobs.job_id=NEW.job_id
                   AND jobs.actor_id=NEW.actor_id
                   AND jobs.tenant_id=NEW.tenant_id
                   AND jobs.conversation_id=NEW.conversation_id
                   AND jobs.channel=NEW.channel
            )
            BEGIN
                SELECT RAISE(ABORT, 'command_job_focus_scope_mismatch');
            END;
            CREATE TABLE IF NOT EXISTS grant_nonces (
                nonce TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                exp INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS confirmation_events (
                handle TEXT PRIMARY KEY,
                payload_json TEXT NOT NULL,
                mac TEXT NOT NULL,
                exp INTEGER NOT NULL,
                consumed INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS confirmation_source_ledger (
                source_key TEXT PRIMARY KEY,
                handle TEXT NOT NULL
            );
            """
        )
        columns = {str(row[1]) for row in self._conn.execute("PRAGMA table_info(jobs)").fetchall()}
        if "source_step_id" not in columns:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute("ALTER TABLE jobs ADD COLUMN source_step_id TEXT NOT NULL DEFAULT ''")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")
        columns = {str(row[1]) for row in self._conn.execute("PRAGMA table_info(jobs)").fetchall()}
        if "source_binding_sha256" not in columns:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute(
                    "ALTER TABLE jobs ADD COLUMN source_binding_sha256 TEXT NOT NULL DEFAULT ''"
                )
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")
        columns = {str(row[1]) for row in self._conn.execute("PRAGMA table_info(jobs)").fetchall()}
        if "cleanup_pending" not in columns:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute("ALTER TABLE jobs ADD COLUMN cleanup_pending INTEGER NOT NULL DEFAULT 0")
                # Any pre-marker row with a durable unit identity may have an
                # interrupted cleanup, including FAILED/COMPLETED rows written
                # by older builds.  DDL and backfill commit atomically, so a
                # crash can never leave the new marker silently clear.
                self._conn.execute(
                    """UPDATE jobs SET cleanup_pending=1
                       WHERE systemd_unit IS NOT NULL AND cgroup_path IS NOT NULL"""
                )
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")
        if "cancel_requested_at" not in columns:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute("ALTER TABLE jobs ADD COLUMN cancel_requested_at REAL")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")
        columns = {str(row[1]) for row in self._conn.execute("PRAGMA table_info(jobs)").fetchall()}
        if "input_manifest_sha256" not in columns:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute(
                    "ALTER TABLE jobs ADD COLUMN input_manifest_sha256 TEXT NOT NULL DEFAULT ''"
                )
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")
        columns = {str(row[1]) for row in self._conn.execute("PRAGMA table_info(jobs)").fetchall()}
        retention_columns = (
            ("stdout_bytes", "INTEGER NOT NULL DEFAULT 0"),
            ("stderr_bytes", "INTEGER NOT NULL DEFAULT 0"),
            ("workspace_retired_at", "REAL"),
        )
        missing_retention = [item for item in retention_columns if item[0] not in columns]
        if missing_retention:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                for name, declaration in missing_retention:
                    self._conn.execute(f"ALTER TABLE jobs ADD COLUMN {name} {declaration}")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")
        columns = {str(row[1]) for row in self._conn.execute("PRAGMA table_info(jobs)").fetchall()}
        if {
            "actor_id",
            "tenant_id",
            "conversation_id",
            "channel",
            "source_row_id",
            "receipt_mac",
            "created_at",
        }.issubset(columns):
            self._conn.executescript(
                """
                CREATE INDEX IF NOT EXISTS idx_command_jobs_scope_status
                    ON jobs(actor_id, tenant_id, conversation_id,channel,status,job_id);
                CREATE TABLE IF NOT EXISTS command_job_publications (
                    job_id TEXT PRIMARY KEY REFERENCES jobs(job_id) ON DELETE RESTRICT,
                    delivery_chat_id TEXT NOT NULL,
                    state TEXT NOT NULL
                        CHECK(state IN ('pending','staged','sent','uncertain','blocked')),
                    notification_id TEXT NOT NULL DEFAULT '',
                    dedup_key TEXT NOT NULL DEFAULT '',
                    envelope_sha256 TEXT NOT NULL DEFAULT '',
                    attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts >= 0),
                    last_error_code TEXT NOT NULL DEFAULT '',
                    next_attempt_at REAL,
                    carrier_retired_at REAL,
                    retention_attempts INTEGER NOT NULL DEFAULT 0 CHECK(retention_attempts >= 0),
                    retention_next_attempt_at REAL,
                    retention_error_code TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_command_job_publications_state
                    ON command_job_publications(state, updated_at, job_id);
                CREATE TABLE IF NOT EXISTS command_job_progress (
                    job_id TEXT PRIMARY KEY REFERENCES jobs(job_id) ON DELETE RESTRICT,
                    checkpoint_sec INTEGER NOT NULL DEFAULT 0
                        CHECK(checkpoint_sec IN (0,60,300,900,1800)),
                    retired_at REAL,
                    stage_attempts INTEGER NOT NULL DEFAULT 0 CHECK(stage_attempts >= 0),
                    stage_next_attempt_at REAL,
                    stage_error_code TEXT NOT NULL DEFAULT '',
                    retire_attempts INTEGER NOT NULL DEFAULT 0 CHECK(retire_attempts >= 0),
                    retire_next_attempt_at REAL,
                    retire_error_code TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_command_job_progress_retired
                    ON command_job_progress(retired_at, checkpoint_sec, job_id);
                """
            )
            publication_columns = {
                str(row[1])
                for row in self._conn.execute("PRAGMA table_info(command_job_publications)").fetchall()
            }
            retention_publication_columns = (
                ("next_attempt_at", "REAL"),
                ("carrier_retired_at", "REAL"),
                ("retention_attempts", "INTEGER NOT NULL DEFAULT 0"),
                ("retention_next_attempt_at", "REAL"),
                ("retention_error_code", "TEXT NOT NULL DEFAULT ''"),
            )
            for name, declaration in retention_publication_columns:
                if name not in publication_columns:
                    self._conn.execute(
                        f"ALTER TABLE command_job_publications ADD COLUMN {name} {declaration}"
                    )
            self._conn.execute(
                """INSERT OR IGNORE INTO command_job_progress(job_id,checkpoint_sec)
                   SELECT job_id,0 FROM command_job_publications"""
            )
            progress_columns = {
                str(row[1])
                for row in self._conn.execute("PRAGMA table_info(command_job_progress)").fetchall()
            }
            progress_retry_columns = (
                ("stage_attempts", "INTEGER NOT NULL DEFAULT 0"),
                ("stage_next_attempt_at", "REAL"),
                ("stage_error_code", "TEXT NOT NULL DEFAULT ''"),
                ("retire_attempts", "INTEGER NOT NULL DEFAULT 0"),
                ("retire_next_attempt_at", "REAL"),
                ("retire_error_code", "TEXT NOT NULL DEFAULT ''"),
            )
            for name, declaration in progress_retry_columns:
                if name not in progress_columns:
                    self._conn.execute(f"ALTER TABLE command_job_progress ADD COLUMN {name} {declaration}")
        # Pending confirmations created by a pre-ledger build cannot prove that
        # their immutable ingress row/update was minted only once. Invalidate
        # them on upgrade instead of silently widening authority.
        self._conn.execute(
            """DELETE FROM confirmation_events
               WHERE NOT EXISTS (
                   SELECT 1 FROM confirmation_source_ledger
                   WHERE confirmation_source_ledger.handle=confirmation_events.handle
               )"""
        )
        self._backfill_blank_job_source_bindings()
        self._init_engineer_work_item_fence_schema()
        self._init_engineer_command_source_slot_schema()

    def _upgrade_source_step_schema_for_provision(self) -> None:
        """Atomically add source-slot columns to an exact pre-lifecycle ledger."""

        columns = {str(row[1]) for row in self._conn.execute("PRAGMA table_info(jobs)")}
        if not columns or {"source_step_id", "source_binding_sha256"}.issubset(columns):
            return
        objects = self._engineer_work_item_fence_schema_objects(self._conn)
        if objects:
            if set(objects) != set(_ENGINEER_WORK_ITEM_FENCE_SCHEMA_OBJECTS):
                raise CommandError("idempotency_fence_schema_invalid")
            for name, (
                expected_type,
                expected_table,
                expected_sql,
            ) in _ENGINEER_WORK_ITEM_FENCE_SCHEMA_OBJECTS.items():
                row = objects[name]
                if name == "trg_engineer_work_item_fence_job_identity_immutable":
                    expected_sql = (
                        _SOURCE_STEP_ONLY_ENGINEER_WORK_ITEM_FENCE_JOB_IDENTITY_IMMUTABLE_SQL
                        if "source_step_id" in columns
                        else _LEGACY_ENGINEER_WORK_ITEM_FENCE_JOB_IDENTITY_IMMUTABLE_SQL
                    )
                if (
                    str(row["type"]) != expected_type
                    or str(row["tbl_name"]) != expected_table
                    or _canonical_schema_sql(str(row["sql"] or "")) != _canonical_schema_sql(expected_sql)
                ):
                    raise CommandError("idempotency_fence_schema_invalid")
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            if objects:
                self._conn.execute("DROP TRIGGER trg_engineer_work_item_fence_job_identity_immutable")
            if "source_step_id" not in columns:
                self._conn.execute("ALTER TABLE jobs ADD COLUMN source_step_id TEXT NOT NULL DEFAULT ''")
            if "source_binding_sha256" not in columns:
                self._conn.execute(
                    "ALTER TABLE jobs ADD COLUMN source_binding_sha256 TEXT NOT NULL DEFAULT ''"
                )
            self._backfill_blank_job_source_bindings(migrate_blank_source_steps=True)
            if objects:
                self._conn.execute(_ENGINEER_WORK_ITEM_FENCE_JOB_IDENTITY_IMMUTABLE_SQL)
            self._conn.execute("COMMIT")
        except Exception:
            if self._conn.in_transaction:
                self._conn.execute("ROLLBACK")
            raise

    def _backfill_blank_job_source_bindings(
        self,
        *,
        migrate_blank_source_steps: bool = False,
    ) -> None:
        columns = {str(row[1]) for row in self._conn.execute("PRAGMA table_info(jobs)")}
        required = {
            "job_id",
            "actor_id",
            "tenant_id",
            "conversation_id",
            "channel",
            "source_row_id",
            "source_step_id",
            "source_binding_sha256",
            "source_hash",
            "telegram_update_id",
        }
        publication_columns = {
            str(row[1]) for row in self._conn.execute("PRAGMA table_info(command_job_publications)")
        }
        if not required.issubset(columns) or not {
            "job_id",
            "delivery_chat_id",
        }.issubset(publication_columns):
            return
        predicate = (
            "jobs.source_binding_sha256='' OR jobs.source_step_id=''"
            if migrate_blank_source_steps
            else "jobs.source_binding_sha256=''"
        )
        rows = self._conn.execute(
            f"""SELECT jobs.job_id,jobs.actor_id,jobs.tenant_id,jobs.conversation_id,
                      jobs.channel,jobs.source_row_id,jobs.source_step_id,jobs.source_hash,
                      jobs.telegram_update_id,jobs.idempotency_key,jobs.command_digest,
                      COALESCE(publication.delivery_chat_id,'') AS delivery_chat_id
                 FROM jobs
                 LEFT JOIN command_job_publications AS publication
                   ON publication.job_id=jobs.job_id
                WHERE {predicate}"""
        ).fetchall()
        for row in rows:
            values = {
                "owner_id": str(row["actor_id"]),
                "tenant_id": str(row["tenant_id"]),
                "conversation_id": str(row["conversation_id"]),
                "channel": str(row["channel"]),
                "source_row_id": str(row["source_row_id"]),
                "source_hash": str(row["source_hash"]),
                "telegram_update_id": str(row["telegram_update_id"]),
                "delivery_chat_id": str(row["delivery_chat_id"]),
            }
            source_step_id = str(row["source_step_id"] or "")
            try:
                if not source_step_id:
                    source_step_id = _historical_source_step_id(
                        job_id=str(row["job_id"]),
                        actor_id=str(row["actor_id"]),
                        idempotency_key=str(row["idempotency_key"]),
                        command_digest=str(row["command_digest"]),
                    )
                source_binding = canonical_engineer_source_binding_sha256(
                    **values,
                    source_step_id=canonical_engineer_source_step_id(source_step_id),
                )
            except (TypeError, ValueError) as exc:
                raise CommandError("engineer_command_source_slot_migration_invalid") from exc
            self._conn.execute(
                "UPDATE jobs SET source_step_id=?,source_binding_sha256=? WHERE job_id=?",
                (source_step_id, source_binding, str(row["job_id"])),
            )

    def _validate_runtime_schema(self) -> None:
        validate_command_store_runtime_schema(self._conn)

    @staticmethod
    def _validate_runtime_schema_connection(connection: sqlite3.Connection) -> None:
        required_tables = {
            "jobs",
            "command_job_focus",
            "grant_nonces",
            "confirmation_events",
            "confirmation_source_ledger",
            "command_job_publications",
            "command_job_progress",
            "engineer_work_item_idempotency_fences",
            "engineer_command_source_slots",
        }
        try:
            tables = {
                str(row[0])
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            }
            if not required_tables.issubset(tables):
                raise CommandError("command_store_schema_invalid")
            publication_columns = {
                str(row["name"]): row
                for row in connection.execute("PRAGMA table_info(command_job_publications)")
            }
            retry_column = publication_columns.get("next_attempt_at")
            if retry_column is None or (
                str(retry_column["type"]).upper(),
                int(retry_column["notnull"]),
                int(retry_column["pk"]),
            ) != ("REAL", 0, 0):
                raise CommandError("command_store_schema_invalid")
            CommandJobStore._validate_command_identity_base_schema(connection)
            CommandJobStore._validate_engineer_work_item_fence_schema(connection)
            CommandJobStore._validate_engineer_command_source_slot_schema(connection)
        except CommandError:
            raise
        except sqlite3.DatabaseError as exc:
            raise CommandError("command_store_schema_invalid") from exc

    def _preflight_engineer_work_item_fence_schema(self) -> None:
        existing_objects = self._engineer_work_item_fence_schema_objects(self._conn)
        if not existing_objects:
            return
        try:
            if set(existing_objects) != set(_ENGINEER_WORK_ITEM_FENCE_SCHEMA_OBJECTS):
                raise CommandError("idempotency_fence_schema_invalid")
            self._validate_command_identity_base_schema(self._conn)
            self._validate_engineer_work_item_fence_schema(self._conn)
        except CommandError:
            raise
        except sqlite3.DatabaseError as exc:
            raise CommandError("idempotency_fence_schema_invalid") from exc

    def _init_engineer_work_item_fence_schema(self) -> None:
        job_columns = {str(row[1]) for row in self._conn.execute("PRAGMA table_info(jobs)").fetchall()}
        required_job_columns = {
            "job_id",
            "actor_id",
            "tenant_id",
            "conversation_id",
            "channel",
            "source_row_id",
            "source_step_id",
            "source_binding_sha256",
            "source_hash",
            "telegram_update_id",
            "idempotency_key",
            "command_digest",
        }
        existing_objects = self._engineer_work_item_fence_schema_objects(self._conn)
        if not required_job_columns.issubset(job_columns):
            if existing_objects:
                raise CommandError("idempotency_fence_schema_invalid")
            # A narrow pre-authority recovery schema is still supported solely
            # so its legacy cgroup can be reaped. It cannot admit jobs or fences.
            return
        publication_columns = {
            str(row[1])
            for row in self._conn.execute("PRAGMA table_info(command_job_publications)").fetchall()
        }
        if not {"job_id", "delivery_chat_id"}.issubset(publication_columns):
            raise CommandError("idempotency_fence_schema_invalid")
        self._validate_command_identity_base_schema(self._conn)
        try:
            if existing_objects:
                # Never heal a partially removed immutable fence schema: doing
                # so could conceal that a raw writer erased permanent truth.
                self._validate_engineer_work_item_fence_schema(self._conn)
                return
            try:
                self._conn.executescript(f"BEGIN IMMEDIATE;\n{_ENGINEER_WORK_ITEM_FENCE_SCHEMA}\nCOMMIT;")
            except sqlite3.DatabaseError:
                if self._conn.in_transaction:
                    self._conn.execute("ROLLBACK")
                raise
            self._validate_engineer_work_item_fence_schema(self._conn)
        except CommandError:
            raise
        except sqlite3.DatabaseError as exc:
            raise CommandError("idempotency_fence_schema_invalid") from exc

    @staticmethod
    def _validate_command_identity_base_schema(connection: sqlite3.Connection) -> None:
        job_columns = {str(row["name"]): row for row in connection.execute("PRAGMA table_info(jobs)")}
        expected_job_columns = {
            "job_id": ("TEXT", 0, 1),
            "actor_id": ("TEXT", 1, 0),
            "tenant_id": ("TEXT", 1, 0),
            "conversation_id": ("TEXT", 1, 0),
            "channel": ("TEXT", 1, 0),
            "source_row_id": ("TEXT", 1, 0),
            "source_step_id": ("TEXT", 1, 0),
            "source_binding_sha256": ("TEXT", 1, 0),
            "source_hash": ("TEXT", 1, 0),
            "telegram_update_id": ("TEXT", 1, 0),
            "idempotency_key": ("TEXT", 1, 0),
            "command_digest": ("TEXT", 1, 0),
        }
        publication_columns = {
            str(row["name"]): row for row in connection.execute("PRAGMA table_info(command_job_publications)")
        }
        expected_publication_columns = {
            "job_id": ("TEXT", 0, 1),
            "delivery_chat_id": ("TEXT", 1, 0),
        }
        for columns, expected in (
            (job_columns, expected_job_columns),
            (publication_columns, expected_publication_columns),
        ):
            for name, identity in expected.items():
                row = columns.get(name)
                if (
                    row is None
                    or (
                        str(row["type"]).upper(),
                        int(row["notnull"]),
                        int(row["pk"]),
                    )
                    != identity
                ):
                    raise CommandError("idempotency_fence_schema_invalid")
        foreign_keys = connection.execute("PRAGMA foreign_key_list(command_job_publications)").fetchall()
        if not any(
            str(row["table"]) == "jobs"
            and str(row["from"]) == "job_id"
            and str(row["to"]) == "job_id"
            and str(row["on_delete"]).upper() == "RESTRICT"
            for row in foreign_keys
        ):
            raise CommandError("idempotency_fence_schema_invalid")
        duplicate_job_identity = connection.execute(
            """SELECT 1 FROM jobs
                GROUP BY actor_id,idempotency_key HAVING count(*)>1 LIMIT 1"""
        ).fetchone()
        if duplicate_job_identity is not None:
            raise CommandError("idempotency_fence_schema_invalid")

    @staticmethod
    def _engineer_work_item_fence_schema_objects(
        connection: sqlite3.Connection,
    ) -> dict[str, sqlite3.Row]:
        rows = connection.execute(
            """SELECT type,name,tbl_name,sql
                 FROM sqlite_master
                WHERE type IN ('table','trigger')
                  AND (
                      name='engineer_work_item_idempotency_fences'
                      OR name GLOB 'trg_engineer_work_item_fence_*'
                  )"""
        ).fetchall()
        return {str(row["name"]): row for row in rows}

    @staticmethod
    def _validate_engineer_work_item_fence_schema(connection: sqlite3.Connection) -> None:
        observed_objects = CommandJobStore._engineer_work_item_fence_schema_objects(connection)
        if set(observed_objects) != set(_ENGINEER_WORK_ITEM_FENCE_SCHEMA_OBJECTS):
            raise CommandError("idempotency_fence_schema_invalid")
        for name, (
            expected_type,
            expected_table,
            expected_sql,
        ) in _ENGINEER_WORK_ITEM_FENCE_SCHEMA_OBJECTS.items():
            row = observed_objects.get(name)
            if (
                row is None
                or str(row["type"]) != expected_type
                or str(row["tbl_name"]) != expected_table
                or _canonical_schema_sql(str(row["sql"] or "")) != _canonical_schema_sql(expected_sql)
            ):
                raise CommandError("idempotency_fence_schema_invalid")
        fence_rows = connection.execute(
            """SELECT actor_id,work_item_id,expected_revision,step_ordinal,
                      source_binding_sha256,idempotency_key,command_digest,created_at
                 FROM engineer_work_item_idempotency_fences"""
        )
        for fence_row in fence_rows:
            _engineer_work_item_fence_projection(fence_row)
        collision = connection.execute(
            """SELECT 1
                 FROM engineer_work_item_idempotency_fences AS fence
                 JOIN jobs
                   ON jobs.actor_id=fence.actor_id
                  AND jobs.idempotency_key=fence.idempotency_key
                LIMIT 1"""
        ).fetchone()
        if collision is not None:
            raise CommandError("idempotency_fence_schema_invalid")

    @staticmethod
    def _engineer_command_source_slot_schema_objects(
        connection: sqlite3.Connection,
    ) -> dict[str, sqlite3.Row]:
        rows = connection.execute(
            """SELECT type,name,tbl_name,sql
                 FROM sqlite_master
                WHERE type IN ('table','trigger')
                  AND (
                      name='engineer_command_source_slots'
                      OR name GLOB 'trg_engineer_command_source_slot_*'
                  )"""
        ).fetchall()
        return {str(row["name"]): row for row in rows}

    def _init_engineer_command_source_slot_schema(self) -> None:
        job_columns = {str(row[1]) for row in self._conn.execute("PRAGMA table_info(jobs)")}
        if not {
            "job_id",
            "actor_id",
            "source_step_id",
            "source_binding_sha256",
            "idempotency_key",
            "command_digest",
            "created_at",
        }.issubset(job_columns):
            if self._engineer_command_source_slot_schema_objects(self._conn):
                raise CommandError("engineer_command_source_slot_schema_invalid")
            return
        if not self._engineer_work_item_fence_schema_objects(self._conn):
            return
        observed = self._engineer_command_source_slot_schema_objects(self._conn)
        if observed:
            missing_fence_guard = set(_ENGINEER_COMMAND_SOURCE_SLOT_SCHEMA_OBJECTS) - {
                "trg_engineer_command_source_slot_fence_insert_guard"
            }
            if set(observed) == missing_fence_guard:
                fence_slot = self._conn.execute(
                    """SELECT 1 FROM engineer_command_source_slots
                        WHERE target_kind='engineer_work_item_fence' LIMIT 1"""
                ).fetchone()
                if fence_slot is None:
                    self._conn.execute(_ENGINEER_COMMAND_SOURCE_SLOT_FENCE_INSERT_GUARD_SQL)
                    self._validate_engineer_command_source_slot_schema(self._conn)
                    return
            self._validate_engineer_command_source_slot_schema(self._conn)
            return
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            self._conn.execute(_ENGINEER_COMMAND_SOURCE_SLOT_TABLE_SQL)
            jobs = self._conn.execute(
                """SELECT jobs.job_id,jobs.actor_id,jobs.tenant_id,
                          jobs.conversation_id,jobs.channel,jobs.source_row_id,
                          jobs.source_step_id,jobs.source_binding_sha256,
                          jobs.source_hash,jobs.telegram_update_id,
                          jobs.idempotency_key,jobs.command_digest,jobs.created_at,
                          COALESCE(publication.delivery_chat_id,'') AS delivery_chat_id
                     FROM jobs
                     LEFT JOIN command_job_publications AS publication
                       ON publication.job_id=jobs.job_id
                    ORDER BY jobs.job_id"""
            ).fetchall()
            legacy_candidates: dict[str, str] = {}
            legacy_counts: dict[tuple[str, str], int] = {}
            for row in jobs:
                job_id = str(row["job_id"])
                actor_id = str(row["actor_id"])
                source_step_id = canonical_engineer_source_step_id(str(row["source_step_id"] or ""))
                if source_step_id != _historical_source_step_id(
                    job_id=job_id,
                    actor_id=actor_id,
                    idempotency_key=str(row["idempotency_key"]),
                    command_digest=str(row["command_digest"]),
                ):
                    continue
                legacy_source = legacy_engineer_source_binding_sha256(
                    owner_id=actor_id,
                    tenant_id=str(row["tenant_id"]),
                    conversation_id=str(row["conversation_id"]),
                    channel=str(row["channel"]),
                    source_row_id=str(row["source_row_id"]),
                    source_hash=str(row["source_hash"]),
                    telegram_update_id=str(row["telegram_update_id"]),
                    delivery_chat_id=str(row["delivery_chat_id"]),
                )
                legacy_candidates[job_id] = legacy_source
                key = (actor_id, legacy_source)
                legacy_counts[key] = legacy_counts.get(key, 0) + 1
            for row in jobs:
                source_binding = str(row["source_binding_sha256"] or "")
                if _LOWER_SHA256_RE.fullmatch(source_binding) is None:
                    raise CommandError("engineer_command_source_slot_migration_invalid")
                actor_id = str(row["actor_id"])
                job_id = str(row["job_id"])
                legacy_candidate = legacy_candidates.get(job_id)
                stored_legacy_alias = (
                    legacy_candidate
                    if legacy_candidate is not None and legacy_counts.get((actor_id, legacy_candidate)) == 1
                    else None
                )
                self._conn.execute(
                    """INSERT INTO engineer_command_source_slots(
                           actor_id,source_binding_sha256,legacy_source_binding_sha256,
                           idempotency_key,command_digest,target_kind,job_id,created_at)
                       VALUES(?,?,?,?,?,'job',?,?)""",
                    (
                        actor_id,
                        source_binding,
                        stored_legacy_alias,
                        str(row["idempotency_key"]),
                        str(row["command_digest"]),
                        job_id,
                        float(row["created_at"]),
                    ),
                )
            fences = self._conn.execute(
                """SELECT actor_id,idempotency_key,work_item_id,expected_revision,
                          step_ordinal,source_binding_sha256,command_digest,created_at
                     FROM engineer_work_item_idempotency_fences
                    ORDER BY actor_id,idempotency_key"""
            ).fetchall()
            for row in fences:
                self._conn.execute(
                    """INSERT INTO engineer_command_source_slots(
                           actor_id,source_binding_sha256,legacy_source_binding_sha256,
                           idempotency_key,command_digest,target_kind,
                           fence_actor_id,fence_idempotency_key,work_item_id,
                           expected_revision,step_ordinal,created_at)
                       VALUES(?,?,?, ?,?,'engineer_work_item_fence', ?,?,?,?,?,?)""",
                    (
                        str(row["actor_id"]),
                        str(row["source_binding_sha256"]),
                        str(row["source_binding_sha256"]),
                        str(row["idempotency_key"]),
                        str(row["command_digest"]),
                        str(row["actor_id"]),
                        str(row["idempotency_key"]),
                        str(row["work_item_id"]),
                        int(row["expected_revision"]),
                        int(row["step_ordinal"]),
                        float(row["created_at"]),
                    ),
                )
            for sql in (
                _ENGINEER_COMMAND_SOURCE_SLOT_INSERT_AUTHORITY_SQL,
                _ENGINEER_COMMAND_SOURCE_SLOT_CROSS_COLLISION_SQL,
                _ENGINEER_COMMAND_SOURCE_SLOT_IMMUTABLE_UPDATE_SQL,
                _ENGINEER_COMMAND_SOURCE_SLOT_IMMUTABLE_DELETE_SQL,
                _ENGINEER_COMMAND_SOURCE_SLOT_JOB_INSERT_GUARD_SQL,
                _ENGINEER_COMMAND_SOURCE_SLOT_FENCE_INSERT_GUARD_SQL,
            ):
                self._conn.execute(sql)
            self._conn.execute("COMMIT")
        except Exception as exc:
            if self._conn.in_transaction:
                self._conn.execute("ROLLBACK")
            if isinstance(exc, CommandError):
                raise
            raise CommandError("engineer_command_source_slot_migration_invalid") from exc
        self._validate_engineer_command_source_slot_schema(self._conn)

    @staticmethod
    def _validate_engineer_command_source_slot_schema(connection: sqlite3.Connection) -> None:
        observed = CommandJobStore._engineer_command_source_slot_schema_objects(connection)
        if set(observed) != set(_ENGINEER_COMMAND_SOURCE_SLOT_SCHEMA_OBJECTS):
            raise CommandError("engineer_command_source_slot_schema_invalid")
        for name, (
            expected_type,
            expected_table,
            expected_sql,
        ) in _ENGINEER_COMMAND_SOURCE_SLOT_SCHEMA_OBJECTS.items():
            row = observed[name]
            if (
                str(row["type"]) != expected_type
                or str(row["tbl_name"]) != expected_table
                or _canonical_schema_sql(str(row["sql"] or "")) != _canonical_schema_sql(expected_sql)
            ):
                raise CommandError("engineer_command_source_slot_schema_invalid")
        slots = connection.execute(
            """SELECT actor_id,source_binding_sha256,legacy_source_binding_sha256,
                      idempotency_key,command_digest,target_kind,job_id,
                      fence_actor_id,fence_idempotency_key,work_item_id,
                      expected_revision,step_ordinal,created_at
                 FROM engineer_command_source_slots"""
        ).fetchall()
        for slot in slots:
            _engineer_command_source_slot_projection(slot)
        job_sources = connection.execute(
            """SELECT jobs.job_id,jobs.source_step_id,jobs.source_binding_sha256,jobs.actor_id,
                      jobs.tenant_id,jobs.conversation_id,jobs.channel,jobs.source_row_id,
                      jobs.source_hash,jobs.telegram_update_id,jobs.idempotency_key,
                      jobs.command_digest,
                      COALESCE(publication.delivery_chat_id,'') AS delivery_chat_id,
                      slot.legacy_source_binding_sha256 AS stored_legacy_source_binding_sha256
                 FROM jobs
                 LEFT JOIN command_job_publications AS publication
                   ON publication.job_id=jobs.job_id
                 LEFT JOIN engineer_command_source_slots AS slot
                   ON slot.target_kind='job' AND slot.job_id=jobs.job_id"""
        ).fetchall()
        legacy_candidates: dict[str, str] = {}
        legacy_counts: dict[tuple[str, str], int] = {}
        for job_source in job_sources:
            values = {
                "owner_id": str(job_source["actor_id"]),
                "tenant_id": str(job_source["tenant_id"]),
                "conversation_id": str(job_source["conversation_id"]),
                "channel": str(job_source["channel"]),
                "source_row_id": str(job_source["source_row_id"]),
                "source_hash": str(job_source["source_hash"]),
                "telegram_update_id": str(job_source["telegram_update_id"]),
                "delivery_chat_id": str(job_source["delivery_chat_id"]),
            }
            source_step_id = str(job_source["source_step_id"] or "")
            try:
                if source_step_id:
                    expected_binding = canonical_engineer_source_binding_sha256(
                        **values,
                        source_step_id=canonical_engineer_source_step_id(source_step_id),
                    )
                    historical = source_step_id == _historical_source_step_id(
                        job_id=str(job_source["job_id"]),
                        actor_id=str(job_source["actor_id"]),
                        idempotency_key=str(job_source["idempotency_key"]),
                        command_digest=str(job_source["command_digest"]),
                    )
                else:
                    expected_binding = legacy_engineer_source_binding_sha256(**values)
                    historical = True
            except (TypeError, ValueError) as exc:
                raise CommandError("engineer_command_source_slot_schema_invalid") from exc
            if str(job_source["source_binding_sha256"]) != expected_binding:
                raise CommandError("engineer_command_source_slot_schema_invalid")
            if historical:
                legacy_source = legacy_engineer_source_binding_sha256(**values)
                job_id = str(job_source["job_id"])
                actor_id = str(job_source["actor_id"])
                legacy_candidates[job_id] = legacy_source
                key = (actor_id, legacy_source)
                legacy_counts[key] = legacy_counts.get(key, 0) + 1
        for job_source in job_sources:
            job_id = str(job_source["job_id"])
            actor_id = str(job_source["actor_id"])
            legacy_candidate = legacy_candidates.get(job_id)
            expected_legacy = (
                legacy_candidate
                if legacy_candidate is not None and legacy_counts.get((actor_id, legacy_candidate)) == 1
                else None
            )
            if job_source["stored_legacy_source_binding_sha256"] != expected_legacy:
                raise CommandError("engineer_command_source_slot_schema_invalid")
        broken = connection.execute(
            """SELECT 1 FROM engineer_command_source_slots AS slot
                WHERE (
                    slot.target_kind='job' AND NOT EXISTS (
                        SELECT 1 FROM jobs
                         WHERE jobs.job_id=slot.job_id
                           AND jobs.actor_id=slot.actor_id
                           AND jobs.source_binding_sha256=slot.source_binding_sha256
                           AND jobs.idempotency_key=slot.idempotency_key
                           AND jobs.command_digest=slot.command_digest
                    )
                ) OR (
                    slot.target_kind='engineer_work_item_fence' AND NOT EXISTS (
                        SELECT 1 FROM engineer_work_item_idempotency_fences AS fence
                         WHERE fence.actor_id=slot.fence_actor_id
                           AND fence.idempotency_key=slot.fence_idempotency_key
                           AND fence.source_binding_sha256=slot.source_binding_sha256
                           AND fence.command_digest=slot.command_digest
                           AND fence.work_item_id=slot.work_item_id
                           AND fence.expected_revision=slot.expected_revision
                           AND fence.step_ordinal=slot.step_ordinal
                    )
                )
                LIMIT 1"""
        ).fetchone()
        orphan = connection.execute(
            """SELECT 1 FROM jobs
                WHERE NOT EXISTS (
                    SELECT 1 FROM engineer_command_source_slots AS slot
                     WHERE slot.target_kind='job'
                       AND slot.job_id=jobs.job_id
                       AND slot.actor_id=jobs.actor_id
                       AND slot.source_binding_sha256=jobs.source_binding_sha256
                       AND slot.idempotency_key=jobs.idempotency_key
                       AND slot.command_digest=jobs.command_digest
                )
                UNION ALL
               SELECT 1 FROM engineer_work_item_idempotency_fences AS fence
                WHERE NOT EXISTS (
                    SELECT 1 FROM engineer_command_source_slots AS slot
                     WHERE slot.target_kind='engineer_work_item_fence'
                       AND slot.actor_id=fence.actor_id
                       AND slot.source_binding_sha256=fence.source_binding_sha256
                       AND slot.idempotency_key=fence.idempotency_key
                       AND slot.command_digest=fence.command_digest
                       AND slot.work_item_id=fence.work_item_id
                       AND slot.expected_revision=fence.expected_revision
                       AND slot.step_ordinal=fence.step_ordinal
                )
                LIMIT 1"""
        ).fetchone()
        cross_collision = connection.execute(
            """SELECT 1
                 FROM engineer_command_source_slots AS left_slot
                 JOIN engineer_command_source_slots AS right_slot
                   ON right_slot.actor_id=left_slot.actor_id
                  AND right_slot.source_binding_sha256<>left_slot.source_binding_sha256
                  AND (
                      right_slot.source_binding_sha256=left_slot.legacy_source_binding_sha256
                      OR right_slot.legacy_source_binding_sha256=left_slot.source_binding_sha256
                  )
                LIMIT 1"""
        ).fetchone()
        if broken is not None or orphan is not None or cross_collision is not None:
            raise CommandError("engineer_command_source_slot_schema_invalid")

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        self._local.acquire()
        _flock(self._lock_fd, _LOCK_EX)
        sequence: int | None = None
        committed = False
        try:
            self._lifecycle.assert_ready(self._conn)
            sequence = self._lifecycle.begin_barrier(self._conn)
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                try:
                    yield self._conn
                except BaseException:
                    self._conn.execute("ROLLBACK")
                    self._lifecycle.abort_barrier(self._conn, sequence)
                    raise
                else:
                    if self.fail_next_commit > 0:
                        self.fail_next_commit -= 1
                        self._conn.execute("ROLLBACK")
                        self._lifecycle.abort_barrier(self._conn, sequence)
                        raise CommandError("durable_write_failed")
                    self._lifecycle.advance_in_transaction(self._conn, sequence)
                    try:
                        self._conn.execute("COMMIT")
                        committed = True
                    except BaseException:
                        if self._conn.in_transaction:
                            self._conn.execute("ROLLBACK")
                        self._lifecycle.abort_barrier(self._conn, sequence)
                        raise
                    self._lifecycle.mark_committed(self._conn, sequence)
                    self._lifecycle.finish_commit(self._conn, sequence)
            except BaseException:
                if self._conn.in_transaction:
                    self._conn.execute("ROLLBACK")
                if (
                    not committed
                    and sequence is not None
                    and (self._lifecycle.pending_path.exists() or self._lifecycle.pending_path.is_symlink())
                ):
                    self._lifecycle.abort_barrier(self._conn, sequence)
                raise
        finally:
            _flock(self._lock_fd, _LOCK_UN)
            self._local.release()

    def job_dir(self, job_id: str) -> Path:
        if not job_id or "/" in job_id or job_id.startswith(".") or len(job_id) > 64:
            raise CommandError("invalid_job_id")
        return self._jobs / job_id

    def _lookup_engineer_work_item_fence(
        self,
        actor_id: str,
        idempotency_key: str,
    ) -> dict[str, str | int] | None:
        row = self._conn.execute(
            """SELECT actor_id,work_item_id,expected_revision,step_ordinal,
                      source_binding_sha256,idempotency_key,command_digest,created_at
                 FROM engineer_work_item_idempotency_fences
                WHERE actor_id=? AND idempotency_key=?""",
            (actor_id, idempotency_key),
        ).fetchone()
        if row is None:
            return None
        return _engineer_work_item_fence_projection(row)

    def lookup_engineer_work_item_fence(
        self,
        actor_id: str,
        idempotency_key: str,
    ) -> dict[str, str | int] | None:
        """Return the exact immutable body-free fence projection, if present."""

        actor, key = _engineer_work_item_fence_key(actor_id, idempotency_key)
        with self._local:
            try:
                return self._lookup_engineer_work_item_fence(actor, key)
            except CommandError:
                raise
            except sqlite3.DatabaseError as exc:
                raise CommandError("idempotency_fence_schema_invalid") from exc

    def lookup_engineer_work_item_fence_by_source(
        self,
        actor_id: str,
        source_binding_sha256: str,
    ) -> dict[str, str | int] | None:
        """Return the one immutable fence bound to an authenticated source."""

        actor, source_binding = _engineer_work_item_fence_source(
            actor_id,
            source_binding_sha256,
        )
        with self._local:
            try:
                row = self._conn.execute(
                    """SELECT actor_id,work_item_id,expected_revision,step_ordinal,
                              source_binding_sha256,idempotency_key,
                              command_digest,created_at
                         FROM engineer_work_item_idempotency_fences
                        WHERE actor_id=? AND source_binding_sha256=?""",
                    (actor, source_binding),
                ).fetchone()
                if row is None:
                    return None
                return _engineer_work_item_fence_projection(row)
            except CommandError:
                raise
            except sqlite3.DatabaseError as exc:
                raise CommandError("idempotency_fence_schema_invalid") from exc

    def _lookup_engineer_command_source_slot(
        self,
        actor_id: str,
        source_binding_sha256: str,
        legacy_source_binding_sha256: str | None = None,
    ) -> dict[str, str | int | None] | None:
        values: list[str] = [actor_id, source_binding_sha256]
        predicate = "actor_id=? AND source_binding_sha256=?"
        if legacy_source_binding_sha256 is not None:
            predicate = "actor_id=? AND (source_binding_sha256=? OR legacy_source_binding_sha256=?)"
            values.append(legacy_source_binding_sha256)
        rows = self._conn.execute(
            f"""SELECT actor_id,source_binding_sha256,legacy_source_binding_sha256,
                       idempotency_key,command_digest,target_kind,job_id,
                       fence_actor_id,fence_idempotency_key,work_item_id,
                       expected_revision,step_ordinal,created_at
                  FROM engineer_command_source_slots WHERE {predicate}
                 ORDER BY source_binding_sha256 LIMIT 2""",
            values,
        ).fetchall()
        if not rows:
            return None
        if len(rows) != 1:
            raise CommandError("engineer_command_source_slot_corrupt")
        return _engineer_command_source_slot_projection(rows[0])

    def lookup_engineer_command_source_slot(
        self,
        actor_id: str,
        source_binding_sha256: str,
        *,
        legacy_source_binding_sha256: str | None = None,
    ) -> dict[str, str | int | None] | None:
        """Resolve v2 source authority, including one conservative legacy alias."""

        actor, source_binding = _engineer_work_item_fence_source(
            actor_id,
            source_binding_sha256,
        )
        legacy_binding = None
        if legacy_source_binding_sha256 is not None:
            _, legacy_binding = _engineer_work_item_fence_source(
                actor,
                legacy_source_binding_sha256,
            )
        with self._local:
            try:
                return self._lookup_engineer_command_source_slot(
                    actor,
                    source_binding,
                    legacy_binding,
                )
            except CommandError:
                raise
            except sqlite3.DatabaseError as exc:
                raise CommandError("engineer_command_source_slot_schema_invalid") from exc

    def lookup_engineer_command_source_slot_by_key(
        self,
        actor_id: str,
        idempotency_key: str,
    ) -> dict[str, str | int | None] | None:
        actor, key = _engineer_work_item_fence_key(actor_id, idempotency_key)
        with self._local:
            try:
                rows = self._conn.execute(
                    """SELECT actor_id,source_binding_sha256,legacy_source_binding_sha256,
                              idempotency_key,command_digest,target_kind,job_id,
                              fence_actor_id,fence_idempotency_key,work_item_id,
                              expected_revision,step_ordinal,created_at
                         FROM engineer_command_source_slots
                        WHERE actor_id=? AND idempotency_key=? LIMIT 2""",
                    (actor, key),
                ).fetchall()
                if not rows:
                    return None
                if len(rows) != 1:
                    raise CommandError("engineer_command_source_slot_corrupt")
                return _engineer_command_source_slot_projection(rows[0])
            except CommandError:
                raise
            except sqlite3.DatabaseError as exc:
                raise CommandError("engineer_command_source_slot_schema_invalid") from exc

    def _claim_engineer_command_source_slot(
        self,
        *,
        actor_id: str,
        source_binding_sha256: str,
        legacy_lookup_sha256: str | None,
        stored_legacy_alias_sha256: str | None,
        idempotency_key: str,
        command_digest: str,
        target_kind: Literal["job", "engineer_work_item_fence"],
        job_id: str | None,
        work_item_id: str | None,
        expected_revision: int | None,
        step_ordinal: int | None,
        created_at: float,
    ) -> None:
        existing_source = self._lookup_engineer_command_source_slot(
            actor_id,
            source_binding_sha256,
            legacy_lookup_sha256,
        )
        existing_key = self.lookup_engineer_command_source_slot_by_key(
            actor_id,
            idempotency_key,
        )
        if existing_source is not None or existing_key is not None:
            existing = existing_source or existing_key
            if existing is not None and existing.get("target_kind") == "engineer_work_item_fence":
                raise CommandError("idempotency_fenced")
            raise CommandError("engineer_command_source_slot_conflict")
        values = (
            actor_id,
            source_binding_sha256,
            stored_legacy_alias_sha256,
            idempotency_key,
            command_digest,
            target_kind,
            job_id,
            actor_id if target_kind == "engineer_work_item_fence" else None,
            idempotency_key if target_kind == "engineer_work_item_fence" else None,
            work_item_id,
            expected_revision,
            step_ordinal,
            float(created_at),
        )
        try:
            with self._source_slot_authority():
                self._conn.execute(
                    """INSERT INTO engineer_command_source_slots(
                           actor_id,source_binding_sha256,legacy_source_binding_sha256,
                           idempotency_key,command_digest,target_kind,job_id,
                           fence_actor_id,fence_idempotency_key,work_item_id,
                           expected_revision,step_ordinal,created_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    values,
                )
        except sqlite3.IntegrityError as exc:
            raise CommandError("engineer_command_source_slot_conflict") from exc

    def create_engineer_work_item_fence(
        self,
        *,
        actor_id: str,
        idempotency_key: str,
        work_item_id: str,
        expected_revision: int,
        step_ordinal: int,
        source_binding_sha256: str,
        legacy_source_binding_sha256: str | None = None,
        command_digest: str,
        created_at: float | None = None,
    ) -> dict[str, str | int]:
        """Commit one permanent fence and verify it again after the commit.

        Callers must wait for this method to return before mutating the main
        work-item database. An already-admitted job wins; an exact fence replay
        returns its original projection without changing the audit timestamp.
        """

        identity = _engineer_work_item_fence_identity(
            actor_id=actor_id,
            idempotency_key=idempotency_key,
            work_item_id=work_item_id,
            expected_revision=expected_revision,
            step_ordinal=step_ordinal,
            source_binding_sha256=source_binding_sha256,
            command_digest=command_digest,
        )
        actor, key, item, revision, ordinal, source_binding, command = identity
        legacy_source_binding = None
        if legacy_source_binding_sha256 is not None:
            _, legacy_source_binding = _engineer_work_item_fence_source(
                actor,
                legacy_source_binding_sha256,
            )
        elif self._strict_runtime:
            raise CommandError("idempotency_fence_source_invalid")
        timestamp = time.time() if created_at is None else created_at
        if (
            isinstance(timestamp, bool)
            or not isinstance(timestamp, (int, float))
            or not math.isfinite(float(timestamp))
            or not 0 <= float(timestamp) <= 253_402_300_799
        ):
            raise CommandError("idempotency_fence_timestamp_invalid")
        expected: dict[str, str | int] = {
            "actor_id": actor,
            "work_item_id": item,
            "expected_revision": revision,
            "step_ordinal": ordinal,
            "source_binding_sha256": source_binding,
            "idempotency_key": key,
            "command_digest": command,
        }
        try:
            with self.transaction():
                job = self._conn.execute(
                    "SELECT 1 FROM jobs WHERE actor_id=? AND idempotency_key=?",
                    (actor, key),
                ).fetchone()
                if job is not None:
                    raise CommandError("idempotency_conflict")
                observed = self._lookup_engineer_work_item_fence(actor, key)
                if observed is None:
                    try:
                        try:
                            self._claim_engineer_command_source_slot(
                                actor_id=actor,
                                source_binding_sha256=source_binding,
                                legacy_lookup_sha256=legacy_source_binding,
                                stored_legacy_alias_sha256=None,
                                idempotency_key=key,
                                command_digest=command,
                                target_kind="engineer_work_item_fence",
                                job_id=None,
                                work_item_id=item,
                                expected_revision=revision,
                                step_ordinal=ordinal,
                                created_at=float(timestamp),
                            )
                        except CommandError as exc:
                            raise CommandError("idempotency_fence_conflict") from exc
                        self._conn.execute(
                            """INSERT INTO engineer_work_item_idempotency_fences(
                                   actor_id,idempotency_key,work_item_id,expected_revision,
                                   step_ordinal,source_binding_sha256,command_digest,created_at
                               ) VALUES(?,?,?,?,?,?,?,?)""",
                            (
                                actor,
                                key,
                                item,
                                revision,
                                ordinal,
                                source_binding,
                                command,
                                float(timestamp),
                            ),
                        )
                    except sqlite3.IntegrityError as exc:
                        if "engineer_work_item_fence_job_exists" in str(exc):
                            raise CommandError("idempotency_conflict") from exc
                        raise CommandError("idempotency_fence_conflict") from exc
                    observed = self._lookup_engineer_work_item_fence(actor, key)
                slot = self._lookup_engineer_command_source_slot(actor, source_binding)
                if slot is None or any(
                    slot.get(name) != value
                    for name, value in (
                        ("target_kind", "engineer_work_item_fence"),
                        ("idempotency_key", key),
                        ("command_digest", command),
                        ("work_item_id", item),
                        ("expected_revision", revision),
                        ("step_ordinal", ordinal),
                    )
                ):
                    raise CommandError("idempotency_fence_conflict")
                if observed != expected:
                    raise CommandError("idempotency_fence_conflict")
        except CommandError:
            raise
        except sqlite3.DatabaseError as exc:
            raise CommandError("idempotency_fence_schema_invalid") from exc

        # This read occurs only after ``transaction`` durably committed. The
        # immutable triggers make any changed/missing result a store failure.
        with self._local:
            confirmed = self._lookup_engineer_work_item_fence(actor, key)
        if confirmed != expected:
            raise CommandError("durable_write_failed")
        return confirmed

    def lookup_idempotency(self, actor_id: str, key: str) -> dict[str, str] | None:
        binding = self.lookup_idempotency_binding(actor_id, key)
        if binding is None:
            return None
        return {
            "job_id": binding["job_id"],
            "digest": binding["command_digest"],
            "delivery_chat_id": binding["delivery_chat_id"],
        }

    def lookup_idempotency_binding(self, actor_id: str, key: str) -> dict[str, str] | None:
        """Return the exact body-free scope bound to an admitted idempotency key."""

        with self._local:
            row = self._conn.execute(
                """SELECT jobs.job_id, jobs.actor_id, jobs.tenant_id,
                          jobs.conversation_id, jobs.channel, jobs.source_row_id,
                          jobs.source_step_id, jobs.source_hash, jobs.telegram_update_id,
                          jobs.idempotency_key, jobs.command_digest,
                          COALESCE(publication.delivery_chat_id, '') AS delivery_chat_id
                     FROM jobs
                     LEFT JOIN command_job_publications AS publication
                       ON publication.job_id=jobs.job_id
                    WHERE jobs.actor_id=? AND jobs.idempotency_key=?""",
                (actor_id, key),
            ).fetchone()
        if row is None:
            return None
        return {
            "job_id": str(row["job_id"]),
            "actor_id": str(row["actor_id"]),
            "tenant_id": str(row["tenant_id"]),
            "conversation_id": str(row["conversation_id"]),
            "channel": str(row["channel"]),
            "source_row_id": str(row["source_row_id"]),
            "source_step_id": str(row["source_step_id"]),
            "source_hash": str(row["source_hash"]),
            "telegram_update_id": str(row["telegram_update_id"]),
            "idempotency_key": str(row["idempotency_key"]),
            "command_digest": str(row["command_digest"]),
            "delivery_chat_id": str(row["delivery_chat_id"]),
        }

    def locked_lookup_idempotency(self, actor_id: str, key: str) -> dict[str, str] | None:
        with self._local:
            return self.lookup_idempotency(actor_id, key)

    def consume_nonce(self, nonce: str, *, exp: int, now: int) -> None:
        self._conn.execute("DELETE FROM grant_nonces WHERE exp<=?", (now,))
        row = self._conn.execute(
            "SELECT kind FROM grant_nonces WHERE nonce=?",
            (nonce,),
        ).fetchone()
        if row is not None:
            raise CommandError("grant_revoked" if str(row["kind"]) == "revoked" else "grant_replay")
        try:
            self._conn.execute(
                "INSERT INTO grant_nonces(nonce, kind, exp) VALUES(?,?,?)",
                (nonce, "used", int(exp)),
            )
        except sqlite3.IntegrityError as exc:
            raise CommandError("grant_replay") from exc

    def nonce_revoked(self, nonce: str) -> bool:
        with self._local:
            row = self._conn.execute(
                "SELECT kind FROM grant_nonces WHERE nonce=?",
                (nonce,),
            ).fetchone()
            return row is not None and str(row["kind"]) == "revoked"

    def revoke_nonce(self, nonce: str, *, exp: int) -> None:
        with self.transaction() as conn:
            conn.execute(
                """INSERT INTO grant_nonces(nonce, kind, exp) VALUES(?,?,?)
                   ON CONFLICT(nonce) DO UPDATE SET kind='revoked', exp=excluded.exp""",
                (nonce, "revoked", int(exp)),
            )

    def insert_confirmation_event(
        self,
        *,
        handle: str,
        payload_json: str,
        mac: str,
        exp: int,
        row_source_key: str,
        update_source_key: str,
    ) -> None:
        try:
            self._conn.executemany(
                "INSERT INTO confirmation_source_ledger(source_key, handle) VALUES(?,?)",
                ((row_source_key, handle), (update_source_key, handle)),
            )
            self._conn.execute(
                "INSERT INTO confirmation_events(handle, payload_json, mac, exp, consumed) VALUES(?,?,?,?,0)",
                (handle, payload_json, mac, int(exp)),
            )
        except sqlite3.IntegrityError as exc:
            raise CommandError("confirmation_replay") from exc

    def take_confirmation_event(self, handle: str, *, now: int) -> dict[str, Any]:
        self._conn.execute("DELETE FROM confirmation_events WHERE exp<=? AND consumed=1", (now,))
        row = self._conn.execute(
            "SELECT handle, payload_json, mac, exp, consumed FROM confirmation_events WHERE handle=?",
            (handle,),
        ).fetchone()
        if row is None:
            raise CommandError("confirmation_event_missing")
        if int(row["consumed"] or 0) != 0:
            raise CommandError("confirmation_replay")
        if int(row["exp"] or 0) <= int(now):
            raise CommandError("confirmation_expired")
        self._conn.execute("UPDATE confirmation_events SET consumed=1 WHERE handle=?", (handle,))
        return {str(key): value for key, value in dict(row).items()}

    def insert_job(self, payload: dict[str, Any]) -> None:
        if not self._conn.in_transaction:
            # Keep the legacy provisioning/test helper safe while making the
            # source-slot claim and job row one durable authority transition.
            with self.transaction():
                self.insert_job(payload)
            return
        source_step_id = payload.get("source_step_id") or ""
        if (source_step_id or self._strict_runtime) and (
            not isinstance(source_step_id, str)
            or _ENGINEER_SOURCE_STEP_ID_RE.fullmatch(source_step_id) is None
        ):
            raise CommandError("invalid_job_source_step")
        delivery_chat_id = str(payload.get("delivery_chat_id") or "")
        source_values = {
            "owner_id": payload.get("actor_id"),
            "tenant_id": payload.get("tenant_id"),
            "conversation_id": payload.get("conversation_id"),
            "channel": payload.get("channel"),
            "source_row_id": payload.get("source_row_id"),
            "source_hash": payload.get("source_hash"),
            "telegram_update_id": payload.get("telegram_update_id"),
            "delivery_chat_id": delivery_chat_id,
        }
        if any(type(value) is not str for value in source_values.values()):
            raise CommandError("invalid_job_source_binding")
        try:
            expected_source_binding = (
                canonical_engineer_source_binding_sha256(
                    **source_values,  # type: ignore[arg-type]
                    source_step_id=canonical_engineer_source_step_id(source_step_id),
                )
                if source_step_id
                else legacy_engineer_source_binding_sha256(
                    **source_values,  # type: ignore[arg-type]
                )
            )
        except (TypeError, ValueError) as exc:
            raise CommandError("invalid_job_source_binding") from exc
        supplied_source_binding = payload.get("source_binding_sha256") or ""
        if self._strict_runtime and not supplied_source_binding:
            raise CommandError("invalid_job_source_binding")
        if supplied_source_binding and supplied_source_binding != expected_source_binding:
            raise CommandError("invalid_job_source_binding")
        source_binding = expected_source_binding
        legacy_source_binding = legacy_engineer_source_binding_sha256(
            **source_values,  # type: ignore[arg-type]
        )
        stored_legacy_alias = source_binding if not source_step_id else None
        columns = (
            "job_id,actor_id,tenant_id,conversation_id,channel,source_row_id,source_step_id,"
            "source_binding_sha256,source_hash,"
            "telegram_update_id,isolation_profile,host_user_authorized,idempotency_key,"
            "command_digest,input_manifest_sha256,argv_sha256,lane,origin,status,error_code,grant_nonce,"
            "timeout_sec,max_stdout_bytes,max_stderr_bytes,created_at,executable_json"
        )
        values = (
            payload["job_id"],
            payload["actor_id"],
            payload["tenant_id"],
            payload["conversation_id"],
            payload["channel"],
            payload["source_row_id"],
            source_step_id,
            source_binding,
            payload["source_hash"],
            payload["telegram_update_id"],
            payload["isolation_profile"],
            1 if payload["host_user_authorized"] else 0,
            payload["idempotency_key"],
            payload["command_digest"],
            payload.get("input_manifest_sha256") or "",
            payload["argv_sha256"],
            payload["lane"],
            payload["origin"],
            payload["status"],
            payload.get("error_code") or "",
            payload["grant_nonce"],
            payload["timeout_sec"],
            payload["max_stdout_bytes"],
            payload["max_stderr_bytes"],
            payload["created_at"],
            payload.get("executable_json"),
        )
        try:
            self._claim_engineer_command_source_slot(
                actor_id=str(payload["actor_id"]),
                source_binding_sha256=source_binding,
                legacy_lookup_sha256=legacy_source_binding,
                stored_legacy_alias_sha256=stored_legacy_alias,
                idempotency_key=str(payload["idempotency_key"]),
                command_digest=str(payload["command_digest"]),
                target_kind="job",
                job_id=str(payload["job_id"]),
                work_item_id=None,
                expected_revision=None,
                step_ordinal=None,
                created_at=float(payload["created_at"]),
            )
            self._conn.execute(
                f"INSERT INTO jobs({columns}) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                values,
            )
            self._set_focus(
                actor_id=str(payload["actor_id"]),
                tenant_id=str(payload["tenant_id"]),
                conversation_id=str(payload["conversation_id"]),
                channel=str(payload["channel"]),
                job_id=str(payload["job_id"]),
                focused_at=float(payload["created_at"]),
                reason="submit",
            )
            if delivery_chat_id:
                created_at = float(payload["created_at"])
                self._conn.execute(
                    """INSERT INTO command_job_publications(
                           job_id,delivery_chat_id,state,created_at,updated_at)
                       VALUES(?,?,'pending',?,?)""",
                    (str(payload["job_id"]), delivery_chat_id, created_at, created_at),
                )
                self._conn.execute(
                    "INSERT INTO command_job_progress(job_id,checkpoint_sec) VALUES(?,0)",
                    (str(payload["job_id"]),),
                )
        except sqlite3.IntegrityError as exc:
            if "engineer_work_item_idempotency_fenced" in str(exc):
                raise CommandError("idempotency_fenced") from exc
            raise CommandError("idempotency_conflict") from exc

    def list_progress_publication_candidates(
        self,
        *,
        now: float,
        limit: int = _PROGRESS_BATCH_MAX,
    ) -> list[dict[str, Any]]:
        """Return only running jobs old enough for at least one sparse checkpoint."""

        bounded = max(1, min(int(limit), _PROGRESS_BATCH_MAX))
        with self._local:
            rows = self._conn.execute(
                """SELECT jobs.*, publication.delivery_chat_id,
                          progress.checkpoint_sec AS progress_checkpoint_sec
                     FROM command_job_progress AS progress
                     JOIN jobs ON jobs.job_id=progress.job_id
                     JOIN command_job_publications AS publication
                       ON publication.job_id=jobs.job_id
                    WHERE progress.retired_at IS NULL
                      AND (progress.stage_next_attempt_at IS NULL
                           OR progress.stage_next_attempt_at<=?)
                      AND jobs.status='running'
                      AND jobs.started_at IS NOT NULL
                      AND (
                          (progress.checkpoint_sec=0 AND jobs.started_at<=?-60)
                          OR (progress.checkpoint_sec=60 AND jobs.started_at<=?-300)
                          OR (progress.checkpoint_sec=300 AND jobs.started_at<=?-900)
                          OR (progress.checkpoint_sec=900 AND jobs.started_at<=?-1800)
                      )
                    ORDER BY CASE WHEN progress.stage_attempts=0 THEN 0 ELSE 1 END,
                             jobs.started_at ASC,jobs.job_id ASC
                    LIMIT ?""",
                (float(now), float(now), float(now), float(now), float(now), bounded),
            ).fetchall()
            return [{str(key): value for key, value in dict(row).items()} for row in rows]

    def advance_progress_checkpoint(
        self,
        job_id: str,
        *,
        previous_checkpoint_sec: int,
        checkpoint_sec: int,
    ) -> bool:
        """CAS one frozen main carrier into the private ledger while the job is running."""

        if (
            previous_checkpoint_sec not in _PROGRESS_CHECKPOINTS
            or checkpoint_sec not in _PROGRESS_CHECKPOINTS - {0}
            or checkpoint_sec <= previous_checkpoint_sec
        ):
            raise CommandError("progress_checkpoint_invalid")
        with self.transaction():
            cursor = self._conn.execute(
                """UPDATE command_job_progress
                      SET checkpoint_sec=?,stage_attempts=0,
                          stage_next_attempt_at=NULL,stage_error_code=''
                    WHERE job_id=? AND checkpoint_sec=? AND retired_at IS NULL
                      AND EXISTS (
                          SELECT 1 FROM jobs
                           WHERE jobs.job_id=command_job_progress.job_id
                             AND jobs.status='running'
                      )""",
                (int(checkpoint_sec), str(job_id), int(previous_checkpoint_sec)),
            )
            if cursor.rowcount == 1:
                return True
            row = self._conn.execute(
                """SELECT progress.checkpoint_sec,progress.retired_at,jobs.status
                     FROM command_job_progress AS progress
                     JOIN jobs ON jobs.job_id=progress.job_id
                    WHERE progress.job_id=?""",
                (str(job_id),),
            ).fetchone()
            if row is None:
                raise CommandError("progress_state_missing")
            return (
                row["retired_at"] is None
                and str(row["status"]) == "running"
                and int(row["checkpoint_sec"]) == checkpoint_sec
            )

    def list_progress_retirement_candidates(
        self,
        *,
        now: float | None = None,
        limit: int = _PROGRESS_BATCH_MAX,
    ) -> list[dict[str, Any]]:
        """Return terminal/unknown scopes whose pending progress must be retired."""

        bounded = max(1, min(int(limit), _PROGRESS_BATCH_MAX))
        moment = time.time() if now is None else float(now)
        with self._local:
            rows = self._conn.execute(
                """SELECT jobs.actor_id,jobs.tenant_id,jobs.conversation_id,jobs.job_id,
                          publication.delivery_chat_id
                     FROM command_job_progress AS progress
                     JOIN jobs ON jobs.job_id=progress.job_id
                     JOIN command_job_publications AS publication
                       ON publication.job_id=jobs.job_id
                    WHERE progress.retired_at IS NULL
                      AND (progress.retire_next_attempt_at IS NULL
                           OR progress.retire_next_attempt_at<=?)
                      AND jobs.status IN ('completed','failed','cancelled','timeout','unknown')
                    ORDER BY CASE WHEN progress.retire_attempts=0 THEN 0 ELSE 1 END,
                             jobs.finished_at ASC,jobs.job_id ASC
                    LIMIT ?""",
                (moment, bounded),
            ).fetchall()
            return [{str(key): value for key, value in dict(row).items()} for row in rows]

    def finish_progress_retirement(self, job_id: str, *, retired_at: float) -> None:
        """Durably stop reconsidering a non-running job after its main queue was swept."""

        with self.transaction():
            cursor = self._conn.execute(
                """UPDATE command_job_progress SET retired_at=COALESCE(retired_at,?)
                    WHERE job_id=? AND retired_at IS NULL
                      AND EXISTS (
                          SELECT 1 FROM jobs
                           WHERE jobs.job_id=command_job_progress.job_id
                             AND jobs.status IN (
                                 'completed','failed','cancelled','timeout','unknown'
                             )
                      )""",
                (float(retired_at), str(job_id)),
            )
            if cursor.rowcount == 1:
                return
            row = self._conn.execute(
                "SELECT retired_at FROM command_job_progress WHERE job_id=?",
                (str(job_id),),
            ).fetchone()
            if row is None or row["retired_at"] is None:
                raise CommandError("progress_state_changed")

    def retire_progress_for_inactive_scope(
        self,
        job_id: str,
        *,
        actor_id: str,
        conversation_id: str,
        retired_at: float,
    ) -> None:
        """Retire progress for an exact archived scope, even while its job runs.

        Archival revokes the publication scope independently of process state.
        The actor/conversation join prevents one stale archive worker from
        retiring another scope, while exact replay of the first durable marker
        remains idempotent.
        """

        if not isinstance(job_id, str) or _JOB_ID.fullmatch(job_id) is None:
            raise CommandError("invalid_job_id")
        if (
            not isinstance(actor_id, str)
            or not actor_id
            or len(actor_id) > 256
            or "\x00" in actor_id
            or not isinstance(conversation_id, str)
            or not conversation_id
            or len(conversation_id) > 256
            or "\x00" in conversation_id
        ):
            raise CommandError("progress_retirement_scope_invalid")
        if (
            isinstance(retired_at, bool)
            or not isinstance(retired_at, (int, float))
            or not math.isfinite(float(retired_at))
            or not 0 <= float(retired_at) <= 253_402_300_799
        ):
            raise CommandError("progress_retirement_time_invalid")
        moment = float(retired_at)
        with self.transaction():
            cursor = self._conn.execute(
                """UPDATE command_job_progress SET retired_at=?
                    WHERE job_id=? AND retired_at IS NULL
                      AND EXISTS (
                          SELECT 1 FROM jobs
                           WHERE jobs.job_id=command_job_progress.job_id
                             AND jobs.actor_id=?
                             AND jobs.conversation_id=?
                      )""",
                (moment, job_id, actor_id, conversation_id),
            )
            if cursor.rowcount == 1:
                return
            row = self._conn.execute(
                """SELECT progress.retired_at,jobs.actor_id,jobs.conversation_id
                     FROM command_job_progress AS progress
                     JOIN jobs ON jobs.job_id=progress.job_id
                    WHERE progress.job_id=?""",
                (job_id,),
            ).fetchone()
            if (
                row is None
                or str(row["actor_id"]) != actor_id
                or str(row["conversation_id"]) != conversation_id
                or row["retired_at"] is None
            ):
                raise CommandError("progress_state_changed")

    def _record_progress_failure(
        self,
        job_id: str,
        *,
        phase: Literal["stage", "retire"],
        error_code: str,
        failed_at: float,
    ) -> None:
        clean = str(error_code or "progress_failed")
        if re.fullmatch(r"[a-z][a-z0-9_]{0,79}", clean) is None:
            clean = "progress_failed"
        if phase == "stage":
            attempts_column = "stage_attempts"
            next_column = "stage_next_attempt_at"
            error_column = "stage_error_code"
        else:
            attempts_column = "retire_attempts"
            next_column = "retire_next_attempt_at"
            error_column = "retire_error_code"
        with self.transaction():
            row = self._conn.execute(
                f"""SELECT {attempts_column} AS attempts FROM command_job_progress
                     WHERE job_id=? AND retired_at IS NULL""",  # nosec B608 - fixed columns above
                (str(job_id),),
            ).fetchone()
            if row is None:
                raise CommandError("progress_state_changed")
            attempts = int(row["attempts"] or 0) + 1
            delay = min(
                _PROGRESS_RETRY_MAX_SEC,
                _PROGRESS_RETRY_BASE_SEC * (2 ** min(attempts - 1, 8)),
            )
            cursor = self._conn.execute(
                f"""UPDATE command_job_progress
                      SET {attempts_column}=?,{next_column}=?,{error_column}=?
                    WHERE job_id=? AND retired_at IS NULL""",  # nosec B608 - fixed columns above
                (attempts, float(failed_at) + delay, clean, str(job_id)),
            )
            if cursor.rowcount != 1:
                raise CommandError("progress_state_changed")

    def record_progress_publication_failure(
        self,
        job_id: str,
        *,
        error_code: str,
        failed_at: float,
    ) -> None:
        self._record_progress_failure(
            job_id,
            phase="stage",
            error_code=error_code,
            failed_at=failed_at,
        )

    def record_progress_retirement_failure(
        self,
        job_id: str,
        *,
        error_code: str,
        failed_at: float,
    ) -> None:
        self._record_progress_failure(
            job_id,
            phase="retire",
            error_code=error_code,
            failed_at=failed_at,
        )

    def list_terminal_publication_candidates(
        self,
        *,
        now: float | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Return bounded terminal/UNKNOWN jobs that still need one honest carrier."""

        moment = time.time() if now is None else float(now)
        with self._local:
            rows = self._conn.execute(
                """SELECT jobs.*, publication.delivery_chat_id,
                          publication.state AS publication_state,
                          publication.notification_id,
                          publication.dedup_key AS publication_dedup_key,
                          publication.envelope_sha256,
                          publication.attempts AS publication_attempts,
                          publication.last_error_code AS publication_error_code,
                          publication.next_attempt_at AS publication_next_attempt_at,
                          publication.updated_at AS publication_updated_at
                     FROM command_job_publications AS publication
                     JOIN jobs ON jobs.job_id=publication.job_id
                    WHERE (
                            (publication.state='pending'
                             AND (publication.next_attempt_at IS NULL
                                  OR publication.next_attempt_at<=?))
                            OR (
                                publication.state='blocked'
                                AND publication.last_error_code='no_generated_files'
                                AND publication.carrier_retired_at IS NULL
                                AND jobs.workspace_retired_at IS NULL
                            )
                          )
                      AND jobs.status IN ('completed','failed','cancelled','timeout','unknown')
                    ORDER BY publication.updated_at ASC, jobs.job_id ASC
                    LIMIT ?""",
                (moment, max(1, min(int(limit), 100))),
            ).fetchall()
            return [{str(key): value for key, value in dict(row).items()} for row in rows]

    def list_staged_publications(self, *, limit: int = 100) -> list[dict[str, Any]]:
        """Return staged rows whose main-queue terminal state needs reconciliation."""

        with self._local:
            rows = self._conn.execute(
                """SELECT jobs.actor_id, jobs.tenant_id, jobs.conversation_id,
                          jobs.source_row_id, jobs.status,
                          publication.*
                     FROM command_job_publications AS publication
                     JOIN jobs ON jobs.job_id=publication.job_id
                    WHERE publication.state='staged'
                    ORDER BY publication.updated_at ASC, publication.job_id ASC
                    LIMIT ?""",
                (max(1, min(int(limit), 500)),),
            ).fetchall()
            return [{str(key): value for key, value in dict(row).items()} for row in rows]

    def list_workspace_retention_candidates(
        self,
        *,
        cutoff: float,
        now: float | None = None,
        limit: int = _RETENTION_BATCH_MAX,
    ) -> list[dict[str, Any]]:
        """Return only old, proven-sent terminal workspaces in a hard-bounded batch."""

        bounded = max(1, min(int(limit), _RETENTION_BATCH_MAX))
        moment = time.time() if now is None else float(now)
        with self._local:
            rows = self._conn.execute(
                """SELECT jobs.*, publication.delivery_chat_id,
                          publication.state AS publication_state,
                          publication.notification_id,
                          publication.dedup_key AS publication_dedup_key,
                          publication.envelope_sha256,
                          publication.last_error_code AS publication_error_code,
                          publication.updated_at AS publication_updated_at
                     FROM command_job_publications AS publication
                     JOIN jobs ON jobs.job_id=publication.job_id
                    WHERE publication.state='sent'
                      AND publication.carrier_retired_at IS NULL
                      AND (publication.retention_next_attempt_at IS NULL
                           OR publication.retention_next_attempt_at<=?)
                      AND publication.updated_at<=?
                      AND jobs.status IN ('completed','failed','cancelled','timeout')
                      AND jobs.cleanup_pending=0
                    ORDER BY CASE WHEN publication.retention_attempts=0 THEN 0 ELSE 1 END,
                             publication.updated_at ASC,jobs.job_id ASC
                    LIMIT ?""",
                (moment, float(cutoff), bounded),
            ).fetchall()
            return [{str(key): value for key, value in dict(row).items()} for row in rows]

    def record_workspace_retention_failure(
        self,
        job_id: str,
        *,
        error_code: str,
        failed_at: float,
    ) -> None:
        """Back off one poison candidate so it cannot starve later eligible jobs."""

        clean = str(error_code or "retention_failed")
        if re.fullmatch(r"[a-z][a-z0-9_]{0,79}", clean) is None:
            clean = "retention_failed"
        with self.transaction():
            row = self._conn.execute(
                """SELECT retention_attempts FROM command_job_publications
                    WHERE job_id=? AND carrier_retired_at IS NULL""",
                (str(job_id),),
            ).fetchone()
            if row is None:
                raise CommandError("retention_authority_changed")
            attempts = int(row["retention_attempts"] or 0) + 1
            delay = min(
                _RETENTION_RETRY_MAX_SEC,
                _RETENTION_RETRY_BASE_SEC * (2 ** min(attempts - 1, 8)),
            )
            cursor = self._conn.execute(
                """UPDATE command_job_publications
                      SET retention_attempts=?,retention_next_attempt_at=?,
                          retention_error_code=?
                    WHERE job_id=? AND carrier_retired_at IS NULL""",
                (attempts, float(failed_at) + delay, clean, str(job_id)),
            )
            if cursor.rowcount != 1:
                raise CommandError("retention_authority_changed")

    def mark_workspace_retirement(
        self,
        job_id: str,
        *,
        notification_id: str,
        dedup_key: str,
        envelope_sha256: str,
        cutoff: float,
        retired_at: float,
        stdout_bytes: int,
        stderr_bytes: int,
    ) -> dict[str, Any]:
        """Persist the irreversible cleanup authority before touching the workspace."""

        if (
            not isinstance(stdout_bytes, int)
            or isinstance(stdout_bytes, bool)
            or stdout_bytes < 0
            or not isinstance(stderr_bytes, int)
            or isinstance(stderr_bytes, bool)
            or stderr_bytes < 0
        ):
            raise CommandError("retention_identity_invalid")
        with self.transaction():
            row = self._conn.execute(
                """SELECT jobs.workspace_retired_at,jobs.stdout_bytes,jobs.stderr_bytes
                     FROM jobs
                     JOIN command_job_publications AS publication
                       ON publication.job_id=jobs.job_id
                    WHERE jobs.job_id=?
                      AND jobs.status IN ('completed','failed','cancelled','timeout')
                      AND jobs.cleanup_pending=0
                      AND publication.state='sent'
                      AND publication.carrier_retired_at IS NULL
                      AND publication.updated_at<=?
                      AND publication.notification_id=?
                      AND publication.dedup_key=?
                      AND publication.envelope_sha256=?""",
                (
                    str(job_id),
                    float(cutoff),
                    str(notification_id),
                    str(dedup_key),
                    str(envelope_sha256),
                ),
            ).fetchone()
            if row is None:
                raise CommandError("retention_authority_changed")
            if row["workspace_retired_at"] is None:
                cursor = self._conn.execute(
                    """UPDATE jobs
                          SET workspace_retired_at=?,stdout_bytes=?,stderr_bytes=?
                        WHERE job_id=? AND workspace_retired_at IS NULL""",
                    (
                        float(retired_at),
                        int(stdout_bytes),
                        int(stderr_bytes),
                        str(job_id),
                    ),
                )
                if cursor.rowcount != 1:
                    raise CommandError("retention_authority_changed")
            elif (
                int(row["stdout_bytes"] or 0) != stdout_bytes or int(row["stderr_bytes"] or 0) != stderr_bytes
            ):
                raise CommandError("retention_identity_changed")
        return self.read_job(job_id)

    def mark_suppressed_workspace_retirement(
        self,
        job_id: str,
        *,
        cutoff: float,
        retired_at: float,
        stdout_bytes: int,
        stderr_bytes: int,
    ) -> dict[str, Any]:
        """Persist cleanup authority for an intentionally carrier-free job."""

        if (
            not isinstance(stdout_bytes, int)
            or isinstance(stdout_bytes, bool)
            or stdout_bytes < 0
            or not isinstance(stderr_bytes, int)
            or isinstance(stderr_bytes, bool)
            or stderr_bytes < 0
        ):
            raise CommandError("retention_identity_invalid")
        with self.transaction():
            row = self._conn.execute(
                """SELECT jobs.workspace_retired_at,jobs.stdout_bytes,jobs.stderr_bytes
                     FROM jobs
                     JOIN command_job_publications AS publication
                       ON publication.job_id=jobs.job_id
                    WHERE jobs.job_id=?
                      AND jobs.status IN ('completed','failed','cancelled','timeout')
                      AND jobs.cleanup_pending=0
                      AND publication.state='blocked'
                      AND publication.last_error_code='no_generated_files'
                      AND publication.carrier_retired_at IS NULL
                      AND publication.updated_at<=?""",
                (str(job_id), float(cutoff)),
            ).fetchone()
            if row is None:
                raise CommandError("retention_authority_changed")
            if row["workspace_retired_at"] is None:
                cursor = self._conn.execute(
                    """UPDATE jobs
                          SET workspace_retired_at=?,stdout_bytes=?,stderr_bytes=?
                        WHERE job_id=? AND workspace_retired_at IS NULL""",
                    (
                        float(retired_at),
                        int(stdout_bytes),
                        int(stderr_bytes),
                        str(job_id),
                    ),
                )
                if cursor.rowcount != 1:
                    raise CommandError("retention_authority_changed")
            elif (
                int(row["stdout_bytes"] or 0) != stdout_bytes or int(row["stderr_bytes"] or 0) != stderr_bytes
            ):
                raise CommandError("retention_identity_changed")
        return self.read_job(job_id)

    def finish_workspace_retirement(
        self,
        job_id: str,
        *,
        notification_id: str,
        dedup_key: str,
        envelope_sha256: str,
        retired_at: float,
    ) -> None:
        """Close the saga while preserving its publication/idempotency tombstone."""

        with self.transaction():
            cursor = self._conn.execute(
                """UPDATE command_job_publications
                      SET carrier_retired_at=COALESCE(carrier_retired_at, ?)
                    WHERE job_id=? AND state='sent' AND carrier_retired_at IS NULL
                      AND notification_id=? AND dedup_key=? AND envelope_sha256=?
                      AND EXISTS (
                          SELECT 1 FROM jobs WHERE jobs.job_id=command_job_publications.job_id
                            AND jobs.workspace_retired_at IS NOT NULL
                      )""",
                (
                    float(retired_at),
                    str(job_id),
                    str(notification_id),
                    str(dedup_key),
                    str(envelope_sha256),
                ),
            )
            if cursor.rowcount == 1:
                return
            row = self._conn.execute(
                """SELECT carrier_retired_at FROM command_job_publications
                    WHERE job_id=? AND state='sent' AND notification_id=?
                      AND dedup_key=? AND envelope_sha256=?""",
                (str(job_id), str(notification_id), str(dedup_key), str(envelope_sha256)),
            ).fetchone()
            if row is None or row["carrier_retired_at"] is None:
                raise CommandError("retention_authority_changed")

    def finish_suppressed_workspace_retirement(self, job_id: str, *, retired_at: float) -> None:
        """Close cleanup while retaining the no-carrier idempotency tombstone."""

        with self.transaction():
            cursor = self._conn.execute(
                """UPDATE command_job_publications
                      SET carrier_retired_at=COALESCE(carrier_retired_at, ?)
                    WHERE job_id=? AND state='blocked'
                      AND last_error_code='no_generated_files'
                      AND carrier_retired_at IS NULL
                      AND EXISTS (
                          SELECT 1 FROM jobs WHERE jobs.job_id=command_job_publications.job_id
                            AND jobs.workspace_retired_at IS NOT NULL
                      )""",
                (float(retired_at), str(job_id)),
            )
            if cursor.rowcount == 1:
                return
            row = self._conn.execute(
                """SELECT carrier_retired_at FROM command_job_publications
                    WHERE job_id=? AND state='blocked'
                      AND last_error_code='no_generated_files'""",
                (str(job_id),),
            ).fetchone()
            if row is None or row["carrier_retired_at"] is None:
                raise CommandError("retention_authority_changed")

    def prune_expired_ephemera(self, *, now: int, limit: int = 100) -> int:
        """Bound short-lived authority rows; immutable source replay tombstones remain."""

        bounded = max(1, min(int(limit), _EPHEMERAL_RETENTION_BATCH_MAX))
        with self.transaction():
            confirmations = self._conn.execute(
                """DELETE FROM confirmation_events
                    WHERE handle IN (
                        SELECT handle FROM confirmation_events
                         WHERE exp<=? ORDER BY exp,handle LIMIT ?
                    )""",
                (int(now), bounded),
            ).rowcount
            nonces = self._conn.execute(
                """DELETE FROM grant_nonces
                    WHERE nonce IN (
                        SELECT nonce FROM grant_nonces
                         WHERE exp<=? ORDER BY exp,nonce LIMIT ?
                    )""",
                (int(now), bounded),
            ).rowcount
        return max(0, int(confirmations)) + max(0, int(nonces))

    def record_publication_attempt(
        self,
        job_id: str,
        error_code: str,
        *,
        failed_at: float | None = None,
        permanent: bool = False,
    ) -> None:
        """Durably back off transient failures; block only a proven permanent one."""

        clean = str(error_code or "publication_failed")
        if re.fullmatch(r"[a-z][a-z0-9_]{0,79}", clean) is None:
            clean = "publication_failed"
        if type(permanent) is not bool:
            raise CommandError("publication_failure_invalid")
        moment = time.time() if failed_at is None else float(failed_at)
        if not math.isfinite(moment):
            raise CommandError("publication_failure_invalid")
        with self.transaction():
            row = self._conn.execute(
                "SELECT attempts FROM command_job_publications WHERE job_id=? AND state='pending'",
                (str(job_id),),
            ).fetchone()
            if row is None:
                raise CommandError("publication_state_changed")
            attempts = min(_PUBLICATION_ATTEMPTS_MAX, int(row["attempts"] or 0) + 1)
            next_attempt_at = None
            state = "blocked" if permanent else "pending"
            if not permanent:
                delay = min(
                    _PUBLICATION_RETRY_MAX_SEC,
                    _PUBLICATION_RETRY_BASE_SEC * (2 ** min(max(attempts - 1, 0), 30)),
                )
                next_attempt_at = moment + delay
            cursor = self._conn.execute(
                """UPDATE command_job_publications
                      SET attempts=?,state=?,last_error_code=?,next_attempt_at=?,updated_at=?
                    WHERE job_id=? AND state='pending'""",
                (attempts, state, clean, next_attempt_at, moment, str(job_id)),
            )
            if cursor.rowcount != 1:
                raise CommandError("publication_state_changed")

    def suppress_empty_publication(self, job_id: str) -> None:
        """Close a no-output publication without creating a delivery carrier.

        ``blocked`` is the existing durable no-retry terminal state.  The
        dedicated reason distinguishes an intentional no-artifact closure from
        a delivery failure, while keeping older ledgers compatible with the
        closed publication-state constraint.
        """

        reason = "no_generated_files"
        with self.transaction():
            cursor = self._conn.execute(
                """UPDATE command_job_publications
                      SET state='blocked',last_error_code=?,next_attempt_at=NULL,updated_at=?
                    WHERE job_id=? AND state='pending'""",
                (reason, time.time(), str(job_id)),
            )
            if cursor.rowcount == 1:
                return
            row = self._conn.execute(
                """SELECT state,last_error_code FROM command_job_publications
                    WHERE job_id=?""",
                (str(job_id),),
            ).fetchone()
            if row is None or not (
                str(row["state"] or "") == "blocked" and str(row["last_error_code"] or "") == reason
            ):
                raise CommandError("publication_state_changed")

    def stage_publication(
        self,
        job_id: str,
        *,
        notification_id: str,
        dedup_key: str,
        envelope_sha256: str,
    ) -> None:
        """Bind the exact committed main-queue row to one terminal command."""

        if (
            not notification_id
            or not dedup_key
            or re.fullmatch(r"[0-9a-f]{64}", str(envelope_sha256 or "")) is None
        ):
            raise CommandError("publication_identity_invalid")
        with self.transaction():
            cursor = self._conn.execute(
                """UPDATE command_job_publications
                      SET state='staged',notification_id=?,dedup_key=?,envelope_sha256=?,
                          last_error_code='',next_attempt_at=NULL,updated_at=?
                    WHERE job_id=?
                      AND (
                          state='pending'
                          OR (
                              state='blocked' AND last_error_code='no_generated_files'
                              AND carrier_retired_at IS NULL
                              AND EXISTS (
                                  SELECT 1 FROM jobs
                                   WHERE jobs.job_id=command_job_publications.job_id
                                     AND jobs.workspace_retired_at IS NULL
                              )
                          )
                      )""",
                (
                    str(notification_id),
                    str(dedup_key),
                    str(envelope_sha256),
                    time.time(),
                    str(job_id),
                ),
            )
            if cursor.rowcount != 1:
                row = self._conn.execute(
                    """SELECT state,notification_id,dedup_key,envelope_sha256
                         FROM command_job_publications WHERE job_id=?""",
                    (str(job_id),),
                ).fetchone()
                if row is None or not (
                    str(row["state"]) == "staged"
                    and str(row["notification_id"]) == str(notification_id)
                    and str(row["dedup_key"]) == str(dedup_key)
                    and str(row["envelope_sha256"]) == str(envelope_sha256)
                ):
                    raise CommandError("publication_state_changed")

    def finish_publication(
        self,
        job_id: str,
        *,
        state: Literal["sent", "uncertain", "blocked"],
    ) -> None:
        """Mirror one proven terminal queue outcome into the command ledger."""

        if state not in _PUBLICATION_STATES - {"pending", "staged"}:
            raise CommandError("publication_state_invalid")
        with self.transaction():
            cursor = self._conn.execute(
                """UPDATE command_job_publications SET state=?,updated_at=?
                    WHERE job_id=? AND state='staged'""",
                (state, time.time(), str(job_id)),
            )
            if cursor.rowcount != 1:
                row = self._conn.execute(
                    "SELECT state FROM command_job_publications WHERE job_id=?",
                    (str(job_id),),
                ).fetchone()
                if row is None or str(row["state"]) != state:
                    raise CommandError("publication_state_changed")

    def reset_staged_publication(self, job_id: str, *, notification_id: str) -> None:
        """Retry only after the main queue proves the prior carrier is gone."""

        with self.transaction():
            cursor = self._conn.execute(
                """UPDATE command_job_publications
                      SET state='pending',notification_id='',dedup_key='',envelope_sha256='',
                          last_error_code='delivery_rejected',next_attempt_at=NULL,updated_at=?
                    WHERE job_id=? AND state='staged' AND notification_id=?""",
                (time.time(), str(job_id), str(notification_id)),
            )
            if cursor.rowcount != 1:
                raise CommandError("publication_state_changed")

    @staticmethod
    def _validate_reference_scope(
        *,
        actor_id: str,
        tenant_id: str,
        conversation_id: str,
        channel: str,
    ) -> tuple[str, str, str, str]:
        values = (actor_id, tenant_id, conversation_id, channel)
        if any(not isinstance(value, str) or not value or "\x00" in value for value in values):
            raise CommandError("invalid_job_scope")
        if any(len(value) > 256 for value in values):
            raise CommandError("invalid_job_scope")
        return values

    def _set_focus(
        self,
        *,
        actor_id: str,
        tenant_id: str,
        conversation_id: str,
        channel: str,
        job_id: str,
        focused_at: float,
        reason: str,
    ) -> None:
        self._conn.execute(
            """INSERT INTO command_job_focus(
                   actor_id,tenant_id,conversation_id,channel,job_id,revision,focused_at,focus_reason)
               VALUES(?,?,?,?,?,1,?,?)
               ON CONFLICT(actor_id,tenant_id,conversation_id,channel) DO UPDATE SET
                   job_id=excluded.job_id,
                   revision=command_job_focus.revision+1,
                   focused_at=excluded.focused_at,
                   focus_reason=excluded.focus_reason""",
            (
                actor_id,
                tenant_id,
                conversation_id,
                channel,
                job_id,
                float(focused_at),
                str(reason),
            ),
        )

    @staticmethod
    def _checked_status(row: sqlite3.Row | dict[str, Any]) -> str:
        status = str(row["status"] or "")
        if status not in _KNOWN_JOB_STATUSES:
            raise CommandError("corrupt_job_state")
        return status

    def _scope_row(
        self,
        job_id: str,
        *,
        actor_id: str,
        tenant_id: str,
        conversation_id: str,
        channel: str,
    ) -> sqlite3.Row:
        row = self._conn.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        if row is None:
            raise CommandError("job_not_found")
        if (
            str(row["actor_id"] or "") != actor_id
            or str(row["tenant_id"] or "") != tenant_id
            or str(row["conversation_id"] or "") != conversation_id
            or str(row["channel"] or "") != channel
        ):
            raise CommandError("job_scope_mismatch")
        self._checked_status(row)
        return row

    def _persist_cancel_intent(self, row: sqlite3.Row, *, requested_at: float) -> None:
        status = self._checked_status(row)
        if status == "unknown":
            raise CommandError("current_job_uncertain")
        if status not in _CANCELLABLE_JOB_STATUSES:
            raise CommandError("job_not_running")
        cursor = self._conn.execute(
            """UPDATE jobs
                  SET cancel_requested_at=COALESCE(cancel_requested_at, ?)
                WHERE job_id=? AND status IN ('planned','admitted','running')""",
            (float(requested_at), str(row["job_id"])),
        )
        if cursor.rowcount != 1:
            raise CommandError("job_not_running")

    def resolve_job_reference(
        self,
        job_id: str | None,
        *,
        actor_id: str,
        tenant_id: str,
        conversation_id: str,
        channel: str,
        operation: Literal["status", "cancel"] = "status",
        requested_at: float | None = None,
    ) -> str:
        """Resolve one explicit/current reference at an exact durable scope.

        No timestamp is an authority signal.  Multiple unresolved jobs are
        ambiguous even when one of them was inserted last.  For cancellation,
        target selection and the durable intent share this transaction.
        """

        actor_id, tenant_id, conversation_id, channel = self._validate_reference_scope(
            actor_id=actor_id,
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            channel=channel,
        )
        if operation not in {"status", "cancel"}:
            raise CommandError("invalid_job_operation")
        if job_id is not None and (not isinstance(job_id, str) or _JOB_ID.fullmatch(job_id) is None):
            raise CommandError("invalid_job_id")
        moment = time.time() if requested_at is None else float(requested_at)
        if not moment >= 0 or moment == float("inf") or moment != moment:
            raise CommandError("invalid_job_time")

        with self.transaction():
            reason = f"explicit_{operation}" if job_id is not None else f"current_{operation}"
            if job_id is not None:
                selected = self._scope_row(
                    job_id,
                    actor_id=actor_id,
                    tenant_id=tenant_id,
                    conversation_id=conversation_id,
                    channel=channel,
                )
            else:
                unresolved = self._conn.execute(
                    """SELECT * FROM jobs
                        WHERE actor_id=? AND tenant_id=? AND conversation_id=? AND channel=?
                          AND status IN ('planned','admitted','running','unknown')
                        ORDER BY job_id LIMIT 2""",
                    (actor_id, tenant_id, conversation_id, channel),
                ).fetchall()
                if len(unresolved) > 1:
                    raise CommandError("current_job_ambiguous")
                if unresolved:
                    selected = unresolved[0]
                else:
                    focus = self._conn.execute(
                        """SELECT jobs.* FROM command_job_focus AS focus
                              LEFT JOIN jobs ON jobs.job_id=focus.job_id
                             WHERE focus.actor_id=? AND focus.tenant_id=?
                               AND focus.conversation_id=? AND focus.channel=?""",
                        (actor_id, tenant_id, conversation_id, channel),
                    ).fetchone()
                    if focus is not None:
                        selected = focus
                        self._scope_row(
                            str(selected["job_id"]),
                            actor_id=actor_id,
                            tenant_id=tenant_id,
                            conversation_id=conversation_id,
                            channel=channel,
                        )
                    else:
                        legacy = self._conn.execute(
                            """SELECT * FROM jobs
                                WHERE actor_id=? AND tenant_id=? AND conversation_id=? AND channel=?
                                ORDER BY job_id LIMIT 2""",
                            (actor_id, tenant_id, conversation_id, channel),
                        ).fetchall()
                        if not legacy:
                            raise CommandError("current_job_not_found")
                        if len(legacy) > 1:
                            raise CommandError("current_job_ambiguous")
                        selected = legacy[0]
                        reason = "legacy_unique"

            selected_id = str(selected["job_id"])
            self._checked_status(selected)
            if operation == "cancel":
                self._persist_cancel_intent(selected, requested_at=moment)
            self._set_focus(
                actor_id=actor_id,
                tenant_id=tenant_id,
                conversation_id=conversation_id,
                channel=channel,
                job_id=selected_id,
                focused_at=moment,
                reason=reason,
            )
            return selected_id

    def persist_cancel_intent(
        self,
        job_id: str,
        *,
        actor_id: str,
        conversation_id: str | None = None,
        requested_at: float | None = None,
    ) -> None:
        """Persist an exact-id cancellation before the in-memory signal."""

        if not isinstance(job_id, str) or _JOB_ID.fullmatch(job_id) is None:
            raise CommandError("invalid_job_id")
        moment = time.time() if requested_at is None else float(requested_at)
        with self.transaction():
            row = self._conn.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            if row is None:
                raise CommandError("job_not_found")
            if str(row["actor_id"] or "") != actor_id:
                raise CommandError("actor_mismatch")
            if conversation_id is not None and str(row["conversation_id"] or "") != conversation_id:
                raise CommandError("conversation_mismatch")
            self._persist_cancel_intent(row, requested_at=moment)

    def cancel_intent_pending(self, job_id: str) -> bool:
        with self._local:
            row = self._conn.execute(
                "SELECT cancel_requested_at FROM jobs WHERE job_id=?",
                (str(job_id),),
            ).fetchone()
            return row is not None and row["cancel_requested_at"] is not None

    def update_job(self, job_id: str, fields: dict[str, Any]) -> None:
        if not fields:
            return
        assignments = []
        values: list[Any] = []
        for key, value in fields.items():
            assignments.append(f"{key}=?")
            values.append(value)
        values.append(job_id)
        self._conn.execute(f"UPDATE jobs SET {', '.join(assignments)} WHERE job_id=?", values)

    def read_job(self, job_id: str) -> dict[str, Any]:
        with self._local:
            row = self._conn.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            if row is None:
                raise CommandError("job_not_found")
            return {str(key): value for key, value in dict(row).items()}

    def list_unreaped(self) -> list[dict[str, Any]]:
        with self._local:
            rows = self._conn.execute(
                """SELECT * FROM jobs
                   WHERE status IN ('admitted','running') OR cleanup_pending=1"""
            ).fetchall()
            return [{str(key): value for key, value in dict(row).items()} for row in rows]


def decode_json_list(raw: str | None) -> list[dict[str, Any]]:
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CommandError("corrupt_job_state") from exc
    if not isinstance(data, list):
        raise CommandError("corrupt_job_state")
    return [item for item in data if isinstance(item, dict)]


def now() -> float:
    return time.time()
