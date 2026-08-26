"""Exact schema-43 durable host-action projection."""

from __future__ import annotations

import sqlite3

HOST_CONTROL_SCHEMA_VERSION = 43

HOST_CONTROL_JOB_SCHEMA = """
CREATE TABLE IF NOT EXISTS host_action_jobs (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id),
    actor_own_id TEXT NOT NULL REFERENCES users(id),
    conversation_id TEXT REFERENCES conversations(id),
    source_message_id TEXT REFERENCES messages(id),
    host_agent_id TEXT NOT NULL,
    capability_id TEXT NOT NULL,
    adapter_id TEXT NOT NULL,
    adapter_version INTEGER NOT NULL CHECK(adapter_version >= 1),
    action_id TEXT NOT NULL,
    normalized_arguments_json TEXT NOT NULL,
    plan_json TEXT NOT NULL,
    plan_digest TEXT NOT NULL CHECK(length(plan_digest) = 64),
    risk_class TEXT NOT NULL
        CHECK(risk_class IN ('local_readonly', 'workspace_transform',
                             'network_observe', 'package_mutation')),
    authorization_basis TEXT NOT NULL,
    approval_id TEXT REFERENCES action_approvals(id),
    idempotency_key TEXT NOT NULL,
    status TEXT NOT NULL
        CHECK(status IN ('planned', 'awaiting_approval', 'approved', 'admitted',
                         'running', 'completed', 'partial', 'failed', 'cancelled',
                         'unknown', 'reconciling', 'reconciled')),
    stage TEXT NOT NULL,
    systemd_unit TEXT,
    result_ref TEXT,
    receipt_ref TEXT,
    error_code TEXT,
    continuation_json TEXT NOT NULL DEFAULT '{}',
    reconciliation_required INTEGER NOT NULL DEFAULT 0
        CHECK(reconciliation_required IN (0, 1)),
    revision INTEGER NOT NULL DEFAULT 1 CHECK(revision >= 1),
    started_at TEXT,
    updated_at TEXT NOT NULL,
    completed_at TEXT,
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(actor_own_id, idempotency_key)
);

CREATE INDEX IF NOT EXISTS idx_host_jobs_actor_status
    ON host_action_jobs(actor_own_id, status, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_host_jobs_agent_status
    ON host_action_jobs(host_agent_id, status, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_host_jobs_reconcile
    ON host_action_jobs(reconciliation_required, status, updated_at);

CREATE TABLE IF NOT EXISTS host_action_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL REFERENCES host_action_jobs(id),
    sequence INTEGER NOT NULL CHECK(sequence >= 1),
    status TEXT NOT NULL,
    stage TEXT NOT NULL,
    outcome_code TEXT NOT NULL,
    receipt_digest TEXT CHECK(receipt_digest IS NULL OR length(receipt_digest) = 64),
    occurred_at TEXT NOT NULL,
    UNIQUE(job_id, sequence)
);

CREATE TRIGGER IF NOT EXISTS host_action_jobs_plan_immutable
BEFORE UPDATE OF user_id, actor_own_id, conversation_id, source_message_id,
                 host_agent_id, capability_id, adapter_id, adapter_version,
                 action_id, normalized_arguments_json, plan_json, plan_digest,
                 risk_class, authorization_basis, idempotency_key,
                 continuation_json, created_at
ON host_action_jobs
BEGIN
    SELECT RAISE(ABORT, 'host action plan identity is immutable');
END;

CREATE TRIGGER IF NOT EXISTS host_action_jobs_transition_guard
BEFORE UPDATE OF status ON host_action_jobs
WHEN NOT (
    OLD.status = NEW.status OR
    (OLD.status = 'planned' AND NEW.status IN ('awaiting_approval', 'admitted', 'failed', 'cancelled')) OR
    (OLD.status = 'awaiting_approval' AND NEW.status IN ('approved', 'failed', 'cancelled')) OR
    (OLD.status = 'approved' AND NEW.status IN ('admitted', 'failed', 'cancelled')) OR
    (OLD.status = 'admitted' AND NEW.status IN ('running', 'failed', 'cancelled', 'unknown')) OR
    (OLD.status = 'running' AND NEW.status IN ('completed', 'partial', 'failed', 'cancelled', 'unknown')) OR
    (OLD.status = 'unknown' AND NEW.status = 'reconciling') OR
    (OLD.status = 'reconciling' AND NEW.status IN ('reconciled', 'unknown'))
)
BEGIN
    SELECT RAISE(ABORT, 'invalid host action status transition');
END;

CREATE TRIGGER IF NOT EXISTS host_action_events_append_only_update
BEFORE UPDATE ON host_action_events
BEGIN
    SELECT RAISE(ABORT, 'host action events are append-only');
END;

CREATE TRIGGER IF NOT EXISTS host_action_events_append_only_delete
BEFORE DELETE ON host_action_events
BEGIN
    SELECT RAISE(ABORT, 'host action events are append-only');
END;
"""


def _canonical_objects() -> dict[tuple[str, str], str]:
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.executescript(HOST_CONTROL_JOB_SCHEMA)
        return {
            (str(row[0]), str(row[1])): "".join(str(row[2]).split())
            for row in conn.execute(
                """SELECT type,name,sql FROM sqlite_master
                   WHERE sql IS NOT NULL
                     AND (name LIKE 'host_action_%' OR tbl_name LIKE 'host_action_%')"""
            )
        }
    finally:
        conn.close()


_EXPECTED_OBJECTS = _canonical_objects()


def validate_host_control_job_schema(
    conn: sqlite3.Connection,
    *,
    required: bool = True,
) -> None:
    """Reject a missing, partial, or weakened durable host-action schema."""

    observed = {
        (str(row[0]), str(row[1])): "".join(str(row[2]).split())
        for row in conn.execute(
            """SELECT type,name,sql FROM sqlite_master
               WHERE sql IS NOT NULL
                 AND (name LIKE 'host_action_%' OR tbl_name LIKE 'host_action_%')"""
        )
    }
    if not observed and not required:
        return
    if observed != _EXPECTED_OBJECTS:
        raise sqlite3.DatabaseError("Schema 43 host-action store is incomplete or altered")

    job_foreign_keys = {
        (str(row[3]), str(row[2]), str(row[4]))
        for row in conn.execute("PRAGMA foreign_key_list(host_action_jobs)")
    }
    if job_foreign_keys != {
        ("user_id", "users", "id"),
        ("actor_own_id", "users", "id"),
        ("conversation_id", "conversations", "id"),
        ("source_message_id", "messages", "id"),
        ("approval_id", "action_approvals", "id"),
    }:
        raise sqlite3.DatabaseError("Schema 43 host-action ownership anchors are invalid")
    event_foreign_keys = {
        (str(row[3]), str(row[2]), str(row[4]))
        for row in conn.execute("PRAGMA foreign_key_list(host_action_events)")
    }
    if event_foreign_keys != {("job_id", "host_action_jobs", "id")}:
        raise sqlite3.DatabaseError("Schema 43 host-action event ownership is invalid")


__all__ = [
    "HOST_CONTROL_JOB_SCHEMA",
    "HOST_CONTROL_SCHEMA_VERSION",
    "validate_host_control_job_schema",
]
