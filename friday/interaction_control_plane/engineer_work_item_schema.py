"""Exact schema-46 projection for dormant ``EngineerWorkItem v1`` state.

The projection is intentionally body-free.  It can remember which authenticated
scope owns a workflow and which opaque command receipts were accepted, but it has
no column capable of storing prompts, model reasoning, argv, output, or paths.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from functools import lru_cache

ENGINEER_WORK_ITEM_SCHEMA_VERSION = 46
ENGINEER_WORK_ITEM_MAX_REVISION = 2_147_483_647
ENGINEER_WORK_ITEM_MAX_STEPS = 4_096
ENGINEER_WORK_ITEM_MAX_TTL_SECONDS = 12 * 60 * 60
ENGINEER_WORK_ITEM_COMPLETION_CONTRACT_SHA256 = hashlib.sha256(
    b"friday.engineer-work-item.v1:verified-terminal-receipt-and-answer-committed"
).hexdigest()

_OPEN_STATES_SQL = "'active','waiting_for_capability','uncertain','waiting_for_input','ready_to_answer'"

ENGINEER_WORK_ITEM_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS engineer_work_items (
    id TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL REFERENCES users(id),
    tenant_id TEXT NOT NULL REFERENCES users(id),
    conversation_id TEXT NOT NULL REFERENCES conversations(id),
    channel TEXT NOT NULL CHECK(channel IN ('telegram')),
    source_binding_sha256 TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN (
        'active','waiting_for_capability','uncertain','waiting_for_input','ready_to_answer',
        'completed','failed','cancelled','expired'
    )),
    revision INTEGER NOT NULL CHECK(revision BETWEEN 1 AND {ENGINEER_WORK_ITEM_MAX_REVISION}),
    step_ordinal INTEGER NOT NULL CHECK(step_ordinal BETWEEN 1 AND {ENGINEER_WORK_ITEM_MAX_STEPS}),
    transition TEXT NOT NULL CHECK(transition IN (
        'created','command_admitted','command_unknown','terminal_observed',
        'next_step_started','answer_ready','completed','failed','cancelled','expired'
    )),
    completion_contract_sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    completed_at TEXT,
    closed_at TEXT,
    CHECK(length(id)=36 AND substr(id,1,4)='ewi_'
          AND substr(id,5) NOT GLOB '*[^0-9a-f]*'),
    CHECK(length(owner_id) BETWEEN 1 AND 200
          AND length(tenant_id) BETWEEN 1 AND 200
          AND length(conversation_id) BETWEEN 1 AND 200),
    CHECK(length(source_binding_sha256)=64
          AND source_binding_sha256 NOT GLOB '*[^0-9a-f]*'),
    CHECK(completion_contract_sha256='{ENGINEER_WORK_ITEM_COMPLETION_CONTRACT_SHA256}'),
    CHECK(strftime('%Y-%m-%dT%H:%M:%S+00:00', unixepoch(created_at), 'unixepoch')=created_at
          AND strftime('%Y-%m-%dT%H:%M:%S+00:00', unixepoch(updated_at), 'unixepoch')=updated_at
          AND strftime('%Y-%m-%dT%H:%M:%S+00:00', unixepoch(expires_at), 'unixepoch')=expires_at
          AND updated_at>=created_at
          AND unixepoch(expires_at)>unixepoch(created_at)
          AND unixepoch(expires_at)-unixepoch(created_at)<={ENGINEER_WORK_ITEM_MAX_TTL_SECONDS}),
    CHECK((state IN ({_OPEN_STATES_SQL})
               AND completed_at IS NULL AND closed_at IS NULL)
          OR (state='completed' AND completed_at=updated_at AND closed_at=updated_at)
          OR (state IN ('failed','cancelled','expired')
               AND completed_at IS NULL AND closed_at=updated_at)),
    CHECK((state='active' AND transition IN ('created','next_step_started'))
          OR (state='waiting_for_capability' AND transition='command_admitted')
          OR (state='uncertain' AND transition='command_unknown')
          OR (state='waiting_for_input' AND transition='terminal_observed')
          OR (state='ready_to_answer' AND transition='answer_ready')
          OR (state='completed' AND transition='completed')
          OR (state='failed' AND transition='failed')
          OR (state='cancelled' AND transition='cancelled')
          OR (state='expired' AND transition='expired'))
);

CREATE TABLE IF NOT EXISTS engineer_work_item_steps (
    work_item_id TEXT NOT NULL REFERENCES engineer_work_items(id) ON DELETE CASCADE,
    owner_id TEXT NOT NULL REFERENCES users(id),
    ordinal INTEGER NOT NULL CHECK(ordinal BETWEEN 1 AND {ENGINEER_WORK_ITEM_MAX_STEPS}),
    state TEXT NOT NULL CHECK(state IN ('prepared','admitted','unknown','settled')),
    idempotency_key TEXT NOT NULL,
    command_digest TEXT NOT NULL,
    job_receipt_sha256 TEXT NOT NULL DEFAULT '',
    terminal_receipt_sha256 TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    admitted_at TEXT,
    settled_at TEXT,
    PRIMARY KEY(work_item_id, ordinal),
    UNIQUE(owner_id, idempotency_key),
    CHECK(length(owner_id) BETWEEN 1 AND 200),
    CHECK(length(idempotency_key)=69 AND substr(idempotency_key,1,5)='ecmd-'
          AND substr(idempotency_key,6) NOT GLOB '*[^0-9a-f]*'),
    CHECK(length(command_digest)=64
          AND command_digest NOT GLOB '*[^0-9a-f]*'),
    CHECK((job_receipt_sha256='') OR
          (length(job_receipt_sha256)=64
           AND job_receipt_sha256 NOT GLOB '*[^0-9a-f]*')),
    CHECK((terminal_receipt_sha256='') OR
          (length(terminal_receipt_sha256)=64
           AND terminal_receipt_sha256 NOT GLOB '*[^0-9a-f]*')),
    CHECK(strftime('%Y-%m-%dT%H:%M:%S+00:00', unixepoch(created_at), 'unixepoch')=created_at
          AND strftime('%Y-%m-%dT%H:%M:%S+00:00', unixepoch(updated_at), 'unixepoch')=updated_at
          AND updated_at>=created_at
          AND (admitted_at IS NULL
               OR (strftime('%Y-%m-%dT%H:%M:%S+00:00',
                            unixepoch(admitted_at), 'unixepoch')=admitted_at
                   AND admitted_at>=created_at AND admitted_at<=updated_at))
          AND (settled_at IS NULL
               OR (strftime('%Y-%m-%dT%H:%M:%S+00:00',
                            unixepoch(settled_at), 'unixepoch')=settled_at
                   AND settled_at>=created_at AND settled_at<=updated_at))),
    CHECK((state='prepared'
               AND length(command_digest)=64 AND job_receipt_sha256=''
               AND terminal_receipt_sha256='' AND admitted_at IS NULL AND settled_at IS NULL)
          OR (state IN ('admitted','unknown')
               AND length(command_digest)=64 AND length(job_receipt_sha256)=64
               AND terminal_receipt_sha256='' AND admitted_at IS NOT NULL AND settled_at IS NULL)
          OR (state='settled'
               AND length(command_digest)=64 AND length(job_receipt_sha256)=64
               AND length(terminal_receipt_sha256)=64
               AND admitted_at IS NOT NULL AND settled_at=updated_at))
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_engineer_work_items_open_scope
    ON engineer_work_items(owner_id, tenant_id, conversation_id, channel)
    WHERE state IN ({_OPEN_STATES_SQL});
CREATE UNIQUE INDEX IF NOT EXISTS uq_engineer_work_items_owner_source
    ON engineer_work_items(owner_id, source_binding_sha256);
CREATE INDEX IF NOT EXISTS idx_engineer_work_items_owner_updated
    ON engineer_work_items(owner_id, updated_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_engineer_work_items_expiry
    ON engineer_work_items(state, expires_at, id);
CREATE INDEX IF NOT EXISTS idx_engineer_work_item_steps_state
    ON engineer_work_item_steps(work_item_id, state, ordinal);

CREATE TRIGGER IF NOT EXISTS trg_engineer_work_item_scope_insert
BEFORE INSERT ON engineer_work_items
WHEN NOT EXISTS (
    SELECT 1 FROM users
     WHERE users.id=NEW.owner_id AND users.status='active'
)
OR NOT EXISTS (
    SELECT 1 FROM users
     WHERE users.id=NEW.tenant_id AND users.status='active'
)
OR NOT EXISTS (
    SELECT 1 FROM conversations
     WHERE conversations.id=NEW.conversation_id
       AND conversations.user_id=NEW.owner_id
       AND conversations.is_archived=0
)
OR EXISTS (
    SELECT 1 FROM work_items
     WHERE work_items.user_id=NEW.owner_id
       AND work_items.conversation_id=NEW.conversation_id
       AND work_items.state IN ('active','waiting_for_input')
)
OR EXISTS (
    SELECT 1 FROM work_item_compare_current_file_web_graphs
     WHERE work_item_compare_current_file_web_graphs.user_id=NEW.owner_id
       AND work_item_compare_current_file_web_graphs.conversation_id=NEW.conversation_id
       AND work_item_compare_current_file_web_graphs.state='active'
)
BEGIN
    SELECT RAISE(ABORT, 'engineer_work_item_scope_mismatch');
END;

CREATE TRIGGER IF NOT EXISTS trg_engineer_work_item_identity_immutable
BEFORE UPDATE ON engineer_work_items
WHEN NEW.id<>OLD.id
  OR NEW.owner_id<>OLD.owner_id
  OR NEW.tenant_id<>OLD.tenant_id
  OR NEW.conversation_id<>OLD.conversation_id
  OR NEW.channel<>OLD.channel
  OR NEW.source_binding_sha256<>OLD.source_binding_sha256
  OR NEW.completion_contract_sha256<>OLD.completion_contract_sha256
  OR NEW.created_at<>OLD.created_at
  OR NEW.expires_at<>OLD.expires_at
BEGIN
    SELECT RAISE(ABORT, 'engineer_work_item_identity_immutable');
END;

CREATE TRIGGER IF NOT EXISTS trg_engineer_work_item_transition_guard
BEFORE UPDATE ON engineer_work_items
WHEN NEW.revision<>OLD.revision+1
  OR NEW.step_ordinal<OLD.step_ordinal
  OR NEW.step_ordinal>OLD.step_ordinal+1
  OR NEW.updated_at<OLD.updated_at
  OR NOT (
      (OLD.state='active' AND NEW.state='waiting_for_capability')
      OR (OLD.state='waiting_for_capability' AND NEW.state IN ('uncertain','waiting_for_input'))
      OR (OLD.state='uncertain' AND NEW.state='waiting_for_input')
      OR (OLD.state='waiting_for_input'
          AND NEW.state IN ('active','ready_to_answer','failed','cancelled','expired'))
      OR (OLD.state='ready_to_answer' AND NEW.state IN ('completed','failed','cancelled','expired'))
  )
BEGIN
    SELECT RAISE(ABORT, 'engineer_work_item_transition_invalid');
END;

CREATE TRIGGER IF NOT EXISTS trg_engineer_work_item_step_insert_guard
BEFORE INSERT ON engineer_work_item_steps
WHEN NOT EXISTS (
    SELECT 1 FROM engineer_work_items
     WHERE engineer_work_items.id=NEW.work_item_id
       AND engineer_work_items.owner_id=NEW.owner_id
       AND engineer_work_items.step_ordinal=NEW.ordinal
       AND engineer_work_items.state='active'
)
BEGIN
    SELECT RAISE(ABORT, 'engineer_work_item_step_scope_invalid');
END;

CREATE TRIGGER IF NOT EXISTS trg_engineer_work_item_step_identity_immutable
BEFORE UPDATE ON engineer_work_item_steps
WHEN NEW.work_item_id<>OLD.work_item_id
  OR NEW.owner_id<>OLD.owner_id
  OR NEW.ordinal<>OLD.ordinal
  OR NEW.idempotency_key<>OLD.idempotency_key
  OR NEW.command_digest<>OLD.command_digest
  OR (OLD.job_receipt_sha256<>'' AND NEW.job_receipt_sha256<>OLD.job_receipt_sha256)
  OR (OLD.terminal_receipt_sha256<>''
      AND NEW.terminal_receipt_sha256<>OLD.terminal_receipt_sha256)
  OR (OLD.admitted_at IS NOT NULL AND NEW.admitted_at IS NOT OLD.admitted_at)
  OR (OLD.settled_at IS NOT NULL AND NEW.settled_at IS NOT OLD.settled_at)
  OR NEW.created_at<>OLD.created_at
BEGIN
    SELECT RAISE(ABORT, 'engineer_work_item_step_identity_immutable');
END;

CREATE TRIGGER IF NOT EXISTS trg_engineer_work_item_step_transition_guard
BEFORE UPDATE ON engineer_work_item_steps
WHEN NEW.updated_at<OLD.updated_at
  OR NOT (
      (OLD.state='prepared' AND NEW.state='admitted')
      OR (OLD.state='admitted' AND NEW.state IN ('unknown','settled'))
      OR (OLD.state='unknown' AND NEW.state='settled')
  )
BEGIN
    SELECT RAISE(ABORT, 'engineer_work_item_step_transition_invalid');
END;

CREATE TRIGGER IF NOT EXISTS trg_engineer_work_item_step_delete_guard
BEFORE DELETE ON engineer_work_item_steps
WHEN EXISTS (
    SELECT 1 FROM engineer_work_items
     WHERE engineer_work_items.id=OLD.work_item_id
)
BEGIN
    SELECT RAISE(ABORT, 'engineer_work_item_step_deletion_immutable');
END;
"""


def _normalize_schema_sql(value: str) -> str:
    return re.sub(r"\s+", "", value)


@lru_cache(maxsize=1)
def _canonical_engineer_work_item_schema_objects() -> dict[tuple[str, str], str]:
    conn = sqlite3.connect(":memory:")
    try:
        conn.executescript(
            """CREATE TABLE users(id TEXT PRIMARY KEY,status TEXT NOT NULL);
               CREATE TABLE conversations(
                   id TEXT PRIMARY KEY,user_id TEXT NOT NULL,is_archived INTEGER NOT NULL
               );
               CREATE TABLE work_items(
                   id TEXT PRIMARY KEY,user_id TEXT,conversation_id TEXT,state TEXT
               );
               CREATE TABLE work_item_compare_current_file_web_graphs(
                   id TEXT PRIMARY KEY,user_id TEXT,conversation_id TEXT,state TEXT
               );"""
        )
        conn.executescript(ENGINEER_WORK_ITEM_SCHEMA)
        return {
            (str(row[0]), str(row[1])): _normalize_schema_sql(str(row[2]))
            for row in conn.execute(
                """SELECT type,name,sql FROM sqlite_master
                     WHERE sql IS NOT NULL
                       AND (name IN ('engineer_work_items','engineer_work_item_steps')
                            OR name LIKE 'trg_engineer_work_item_%'
                            OR tbl_name IN ('engineer_work_items','engineer_work_item_steps'))
                     ORDER BY type,name"""
            )
        }
    finally:
        conn.close()


def validate_engineer_work_item_schema(
    conn: sqlite3.Connection,
    *,
    required: bool = True,
) -> None:
    """Fail closed on a missing, weakened, partial, or internally inconsistent store."""

    related = {
        (str(row[0]), str(row[1])): _normalize_schema_sql(str(row[2]))
        for row in conn.execute(
            """SELECT type,name,sql FROM sqlite_master
                 WHERE sql IS NOT NULL
                   AND (name IN ('engineer_work_items','engineer_work_item_steps')
                        OR name LIKE 'trg_engineer_work_item_%'
                        OR tbl_name IN ('engineer_work_items','engineer_work_item_steps'))
                 ORDER BY type,name"""
        )
    }
    if not related:
        if required:
            raise sqlite3.DatabaseError("Schema 46 Engineer Work Item store is missing")
        return
    if related != _canonical_engineer_work_item_schema_objects():
        raise sqlite3.DatabaseError("Schema 46 Engineer Work Item DDL is incomplete or altered")

    for table in ("engineer_work_items", "engineer_work_item_steps"):
        quick_check = conn.execute(f'PRAGMA quick_check("{table}")').fetchone()  # nosec B608
        if quick_check is None or str(quick_check[0]) != "ok":
            raise sqlite3.DatabaseError("Schema 46 Engineer Work Item rows violate constraints")

    mismatched = conn.execute(
        """SELECT 1
              FROM engineer_work_items AS item
              LEFT JOIN engineer_work_item_steps AS step
                ON step.work_item_id=item.id AND step.ordinal=item.step_ordinal
             WHERE step.work_item_id IS NULL
                OR step.owner_id<>item.owner_id
                OR NOT EXISTS (
                    SELECT 1 FROM conversations conversation
                     WHERE conversation.id=item.conversation_id
                       AND conversation.user_id=item.owner_id
                )
                OR (item.state='active' AND step.state<>'prepared')
                OR (item.state='waiting_for_capability' AND step.state<>'admitted')
                OR (item.state='uncertain' AND step.state<>'unknown')
                OR (item.state IN ('waiting_for_input','ready_to_answer','completed')
                    AND step.state<>'settled')
                OR (item.state IN ('failed','cancelled','expired')
                    AND step.state<>'settled')
                OR (SELECT COUNT(*) FROM engineer_work_item_steps all_steps
                     WHERE all_steps.work_item_id=item.id)<>item.step_ordinal
                OR EXISTS (
                    SELECT 1 FROM engineer_work_item_steps all_steps
                     WHERE all_steps.work_item_id=item.id
                       AND (all_steps.owner_id<>item.owner_id
                            OR all_steps.created_at<item.created_at
                            OR all_steps.updated_at>item.updated_at
                            OR (all_steps.ordinal<item.step_ordinal
                                AND all_steps.state<>'settled'))
                )
             LIMIT 1"""
    ).fetchone()
    if mismatched is not None:
        raise sqlite3.DatabaseError("Schema 46 Engineer Work Item rows are inconsistent")


__all__ = [
    "ENGINEER_WORK_ITEM_MAX_REVISION",
    "ENGINEER_WORK_ITEM_COMPLETION_CONTRACT_SHA256",
    "ENGINEER_WORK_ITEM_MAX_STEPS",
    "ENGINEER_WORK_ITEM_MAX_TTL_SECONDS",
    "ENGINEER_WORK_ITEM_SCHEMA",
    "ENGINEER_WORK_ITEM_SCHEMA_VERSION",
    "validate_engineer_work_item_schema",
]
