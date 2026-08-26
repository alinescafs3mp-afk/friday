"""Small durable admission ledger for host-agent execution jobs."""

from __future__ import annotations

import os
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from friday.host_control.contracts import canonical_json_bytes, decode_canonical_json

_JOB_ID = re.compile(r"^h?job_[0-9a-f]{16,64}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_IDEMPOTENCY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_ACTOR = _IDEMPOTENCY
_STATUSES = frozenset({"admitted", "running", "completed", "partial", "failed", "cancelled", "unknown"})


class AgentJobConflict(RuntimeError):
    """A job/idempotency identity is already bound to another plan."""


class AgentJobStateError(RuntimeError):
    """A stale agent-side lifecycle transition was refused."""


class AgentJobStore:
    """Persist plan admission before execution and terminal metadata afterwards.

    Raw stdout/stderr never enters this database.  It holds only a bounded
    semantic response containing hashes and evidence references.  Jobs left in
    ``admitted``/``running`` by a daemon crash become explicitly ``unknown`` on
    reopen, with a durable reason that directs callers to reconciliation.
    """

    def __init__(self, database: str | Path) -> None:
        selected = Path(database)
        if (
            not selected.is_absolute()
            or selected.is_symlink()
            or selected.parent.is_symlink()
            or str(selected.parent.resolve(strict=True)) != str(selected.parent)
        ):
            raise ValueError("agent job database path must be canonical and absolute")
        if selected.exists():
            observed = selected.lstat()
            if observed.st_uid != os.geteuid() or observed.st_nlink != 1:
                raise ValueError("agent job database has unsafe ownership")
        self._lock = threading.Lock()
        self._connection = sqlite3.connect(str(selected), check_same_thread=False, isolation_level=None)
        self._connection.row_factory = sqlite3.Row
        os.chmod(selected, 0o600)
        self._connection.execute("PRAGMA busy_timeout=5000")
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute(
            """CREATE TABLE IF NOT EXISTS agent_jobs (
                   job_id TEXT PRIMARY KEY,
                   idempotency_key TEXT NOT NULL UNIQUE,
                   plan_digest TEXT NOT NULL,
                   actor_id TEXT NOT NULL,
                   own_id TEXT NOT NULL,
                   status TEXT NOT NULL CHECK(status IN
                       ('admitted','running','completed','partial','failed','cancelled','unknown')),
                   result_json BLOB,
                   revision INTEGER NOT NULL DEFAULT 1,
                   created_at INTEGER NOT NULL,
                   updated_at INTEGER NOT NULL
               )"""
        )
        self._connection.execute(
            """CREATE TRIGGER IF NOT EXISTS agent_jobs_identity_immutable
               BEFORE UPDATE OF job_id,idempotency_key,plan_digest,actor_id,own_id,created_at ON agent_jobs
               BEGIN SELECT RAISE(ABORT, 'agent job identity is immutable'); END"""
        )
        self._mark_interrupted_jobs_unknown()

    def _mark_interrupted_jobs_unknown(self) -> None:
        now = int(time.time())
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            rows = self._connection.execute(
                "SELECT job_id FROM agent_jobs WHERE status IN ('admitted','running')"
            ).fetchall()
            for row in rows:
                job_id = str(row["job_id"])
                result = canonical_json_bytes(
                    {
                        "error_code": "daemon_restart_during_execution",
                        "job_id": job_id,
                        "reconciliation_required": True,
                        "status": "unknown",
                    },
                    maximum=512 * 1024,
                )
                self._connection.execute(
                    """UPDATE agent_jobs
                           SET status='unknown',result_json=?,revision=revision+1,updated_at=?
                           WHERE job_id=? AND status IN ('admitted','running')""",
                    (result, now, job_id),
                )
            self._connection.execute("COMMIT")
        except Exception:
            if self._connection.in_transaction:
                self._connection.execute("ROLLBACK")
            raise

    @staticmethod
    def _validate_identity(
        job_id: str,
        idempotency_key: str,
        plan_digest: str,
        actor_id: str,
        own_id: str,
    ) -> None:
        if _JOB_ID.fullmatch(job_id) is None:
            raise ValueError("agent job id is invalid")
        if _IDEMPOTENCY.fullmatch(idempotency_key) is None:
            raise ValueError("agent job idempotency key is invalid")
        if _DIGEST.fullmatch(plan_digest) is None:
            raise ValueError("agent job plan digest is invalid")
        if _ACTOR.fullmatch(actor_id) is None or _ACTOR.fullmatch(own_id) is None:
            raise ValueError("agent job actor identity is invalid")

    @staticmethod
    def _decode(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        record = dict(row)
        raw = record.pop("result_json", None)
        record["result"] = decode_canonical_json(bytes(raw)) if raw is not None else None
        return record

    def admit(
        self,
        *,
        job_id: str,
        idempotency_key: str,
        plan_digest: str,
        actor_id: str,
        own_id: str,
    ) -> tuple[dict[str, Any], bool]:
        self._validate_identity(job_id, idempotency_key, plan_digest, actor_id, own_id)
        now = int(time.time())
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                existing = self._connection.execute(
                    "SELECT * FROM agent_jobs WHERE job_id=? OR idempotency_key=?",
                    (job_id, idempotency_key),
                ).fetchone()
                if existing is not None:
                    if (
                        str(existing["job_id"]) != job_id
                        or str(existing["idempotency_key"]) != idempotency_key
                        or str(existing["plan_digest"]) != plan_digest
                        or str(existing["actor_id"]) != actor_id
                        or str(existing["own_id"]) != own_id
                    ):
                        raise AgentJobConflict("job identity is already bound to a different plan")
                    self._connection.execute("COMMIT")
                    decoded = self._decode(existing)
                    assert decoded is not None
                    return decoded, False
                self._connection.execute(
                    """INSERT INTO agent_jobs(
                           job_id,idempotency_key,plan_digest,actor_id,own_id,
                           status,created_at,updated_at)
                       VALUES(?,?,?,?,?,'admitted',?,?)""",
                    (job_id, idempotency_key, plan_digest, actor_id, own_id, now, now),
                )
                self._connection.execute("COMMIT")
            except Exception:
                if self._connection.in_transaction:
                    self._connection.execute("ROLLBACK")
                raise
        record = self.get(job_id)
        assert record is not None
        return record, True

    def get(self, job_id: str) -> dict[str, Any] | None:
        if _JOB_ID.fullmatch(str(job_id)) is None:
            return None
        with self._lock:
            return self._decode(
                self._connection.execute("SELECT * FROM agent_jobs WHERE job_id=?", (job_id,)).fetchone()
            )

    def running_count(self) -> int:
        with self._lock:
            row = self._connection.execute(
                "SELECT COUNT(*) FROM agent_jobs WHERE status='running'"
            ).fetchone()
        return int(row[0]) if row is not None else 0

    def transition(
        self,
        job_id: str,
        *,
        expected: tuple[str, ...],
        status: str,
        result: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if status not in _STATUSES or not expected or any(item not in _STATUSES for item in expected):
            raise ValueError("agent job transition status is invalid")
        encoded = None if result is None else canonical_json_bytes(result, maximum=512 * 1024)
        placeholders = ",".join("?" for _item in expected)
        parameters: tuple[Any, ...] = (
            status,
            encoded,
            int(time.time()),
            job_id,
            *expected,
        )
        with self._lock:
            cursor = self._connection.execute(
                f"""UPDATE agent_jobs
                       SET status=?,result_json=?,revision=revision+1,updated_at=?
                       WHERE job_id=? AND status IN ({placeholders})""",  # nosec B608 - constants only
                parameters,
            )
            if cursor.rowcount != 1:
                raise AgentJobStateError("agent job transition is stale")
        record = self.get(job_id)
        if record is None:
            raise AgentJobStateError("agent job disappeared")
        return record

    def close(self) -> None:
        with self._lock:
            self._connection.close()


__all__ = ["AgentJobConflict", "AgentJobStateError", "AgentJobStore"]
