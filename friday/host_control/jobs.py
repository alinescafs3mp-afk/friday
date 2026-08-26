"""Durable, actor-scoped host-action lifecycle storage."""

from __future__ import annotations

import json
import re
import sqlite3
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_JOB_ID = re.compile(r"^hjob_[0-9a-f]{32}$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_ACTIVE = frozenset(
    {
        "planned",
        "awaiting_approval",
        "approved",
        "admitted",
        "running",
        "unknown",
        "reconciling",
    }
)
_TERMINAL = frozenset({"completed", "partial", "failed", "cancelled", "reconciled"})


class HostJobConflict(RuntimeError):
    """An idempotency key already names a different immutable plan."""


class HostJobTransitionError(RuntimeError):
    """A stale or invalid lifecycle transition was refused."""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _canonical_json(value: Mapping[str, Any] | None) -> str:
    return json.dumps(value or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _identifier(value: object, *, field: str) -> str:
    rendered = str(value or "").strip()
    if _IDENTIFIER.fullmatch(rendered) is None:
        raise ValueError(f"{field} is invalid")
    return rendered


def _decode_row(row: Any) -> dict[str, Any] | None:
    if row is None:
        return None
    result = dict(row)
    for source, target in (
        ("normalized_arguments_json", "normalized_arguments"),
        ("plan_json", "plan"),
        ("continuation_json", "continuation"),
    ):
        try:
            decoded = json.loads(str(result.get(source) or "{}"))
        except (TypeError, ValueError):
            decoded = {}
        result[target] = decoded if isinstance(decoded, dict) else {}
    result["reconciliation_required"] = bool(result.get("reconciliation_required"))
    return result


class HostJobStore:
    """Small service around the exact schema-43 tables.

    The store keeps large stdout/stderr/XML out of SQLite. ``result_ref`` and
    ``receipt_ref`` name bounded evidence files in the per-job workspace.
    """

    def __init__(self, storage: Any) -> None:
        self._storage = storage

    def get(
        self,
        job_id: str,
        *,
        user_id: str,
        actor_own_id: str,
    ) -> dict[str, Any] | None:
        return _decode_row(
            self._storage.execute(
                """SELECT * FROM host_action_jobs
                   WHERE id=? AND user_id=? AND actor_own_id=?""",
                (str(job_id), str(user_id), str(actor_own_id)),
            ).fetchone()
        )

    def create_or_get(
        self,
        *,
        user_id: str,
        actor_own_id: str,
        conversation_id: str | None,
        source_message_id: str | None,
        host_agent_id: str,
        capability_id: str,
        adapter_id: str,
        adapter_version: int,
        action_id: str,
        normalized_arguments: Mapping[str, Any],
        plan: Mapping[str, Any],
        plan_digest: str,
        risk_class: str,
        authorization_basis: str,
        idempotency_key: str,
        continuation: Mapping[str, Any] | None = None,
        awaiting_approval: bool = False,
        ttl_sec: int = 900,
        job_id: str | None = None,
    ) -> tuple[dict[str, Any], bool]:
        digest = str(plan_digest or "").strip().casefold()
        if _DIGEST.fullmatch(digest) is None:
            raise ValueError("plan_digest is invalid")
        agent = _identifier(host_agent_id, field="host_agent_id")
        capability = _identifier(capability_id, field="capability_id")
        adapter = _identifier(adapter_id, field="adapter_id")
        action = _identifier(action_id, field="action_id")
        idem = _identifier(idempotency_key, field="idempotency_key")
        if not 1 <= int(adapter_version) <= 1_000_000:
            raise ValueError("adapter_version is invalid")
        normalized_json = _canonical_json(normalized_arguments)
        plan_json = _canonical_json(plan)
        continuation_json = _canonical_json(continuation)
        now = _now()
        expires = (datetime.now(UTC) + timedelta(seconds=max(30, min(int(ttl_sec), 86_400)))).isoformat()
        status = "awaiting_approval" if awaiting_approval else "planned"
        stage = "approval" if awaiting_approval else "plan"
        selected_job_id = f"hjob_{uuid.uuid4().hex}" if job_id is None else str(job_id)
        if _JOB_ID.fullmatch(selected_job_id) is None:
            raise ValueError("host action job id is invalid")

        self._storage.ensure_user(user_id)
        if actor_own_id != user_id:
            self._storage.ensure_user(actor_own_id)
        with self._storage.transaction() as conn:
            existing = conn.execute(
                """SELECT * FROM host_action_jobs
                   WHERE actor_own_id=? AND idempotency_key=?""",
                (actor_own_id, idem),
            ).fetchone()
            if existing is not None:
                found = _decode_row(existing)
                assert found is not None
                if str(found.get("plan_digest") or "") != digest:
                    raise HostJobConflict("idempotency key already binds a different host action plan")
                return found, False
            conn.execute(
                """INSERT INTO host_action_jobs(
                       id,user_id,actor_own_id,conversation_id,source_message_id,
                       host_agent_id,capability_id,adapter_id,adapter_version,action_id,
                       normalized_arguments_json,plan_json,plan_digest,risk_class,
                       authorization_basis,idempotency_key,status,stage,
                       continuation_json,reconciliation_required,revision,
                       updated_at,expires_at,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    selected_job_id,
                    user_id,
                    actor_own_id,
                    conversation_id,
                    source_message_id,
                    agent,
                    capability,
                    adapter,
                    int(adapter_version),
                    action,
                    normalized_json,
                    plan_json,
                    digest,
                    str(risk_class),
                    str(authorization_basis)[:256],
                    idem,
                    status,
                    stage,
                    continuation_json,
                    0,
                    1,
                    now,
                    expires,
                    now,
                ),
            )
            conn.execute(
                """INSERT INTO host_action_events(
                       job_id,sequence,status,stage,outcome_code,occurred_at)
                   VALUES(?,1,?,?,?,?)""",
                (selected_job_id, status, stage, "created", now),
            )
        created = self.get(selected_job_id, user_id=user_id, actor_own_id=actor_own_id)
        if created is None:
            raise RuntimeError("host action job was not persisted")
        return created, True

    def bind_approval(
        self,
        job_id: str,
        approval_id: str,
        *,
        user_id: str,
        actor_own_id: str,
    ) -> dict[str, Any]:
        with self._storage.transaction() as conn:
            cursor = conn.execute(
                """UPDATE host_action_jobs SET approval_id=?, revision=revision+1, updated_at=?
                   WHERE id=? AND user_id=? AND actor_own_id=?
                     AND status='awaiting_approval' AND approval_id IS NULL""",
                (str(approval_id), _now(), job_id, user_id, actor_own_id),
            )
        if cursor.rowcount != 1:
            raise HostJobTransitionError("host action approval binding is stale")
        row = self.get(job_id, user_id=user_id, actor_own_id=actor_own_id)
        if row is None:
            raise HostJobTransitionError("host action job disappeared")
        return row

    def close_rejected_approval(
        self,
        job_id: str,
        approval_id: str,
        *,
        user_id: str,
        actor_own_id: str,
    ) -> dict[str, Any]:
        """Close a human-rejected job without ever entering an effect stage."""

        current = self.get(job_id, user_id=user_id, actor_own_id=actor_own_id)
        if (
            current is None
            or current.get("status") != "awaiting_approval"
            or str(current.get("approval_id") or "") != str(approval_id)
        ):
            raise HostJobTransitionError("rejected host approval does not match an awaiting job")
        return self.transition(
            job_id,
            user_id=user_id,
            actor_own_id=actor_own_id,
            expected_status="awaiting_approval",
            status="cancelled",
            stage="approval",
            outcome_code="approval_rejected",
            error_code="approval_rejected",
        )

    def transition(
        self,
        job_id: str,
        *,
        user_id: str,
        actor_own_id: str,
        expected_status: str,
        status: str,
        stage: str,
        outcome_code: str,
        systemd_unit: str | None = None,
        result_ref: str | None = None,
        receipt_ref: str | None = None,
        receipt_digest: str | None = None,
        error_code: str | None = None,
    ) -> dict[str, Any]:
        if status not in _ACTIVE | _TERMINAL:
            raise ValueError("unknown host action status")
        if receipt_digest is not None and _DIGEST.fullmatch(str(receipt_digest)) is None:
            raise ValueError("receipt_digest is invalid")
        now = _now()
        terminal = status in _TERMINAL
        reconciliation = status in {"unknown", "reconciling"}
        with self._storage.transaction() as conn:
            current = conn.execute(
                """SELECT revision FROM host_action_jobs
                   WHERE id=? AND user_id=? AND actor_own_id=? AND status=?""",
                (job_id, user_id, actor_own_id, expected_status),
            ).fetchone()
            if current is None:
                raise HostJobTransitionError("host action transition is stale")
            revision = int(current[0])
            try:
                cursor = conn.execute(
                    """UPDATE host_action_jobs
                   SET status=?,stage=?,systemd_unit=COALESCE(?,systemd_unit),
                       result_ref=COALESCE(?,result_ref),receipt_ref=COALESCE(?,receipt_ref),
                       error_code=?,reconciliation_required=?,revision=revision+1,
                       started_at=CASE WHEN ?='running' AND started_at IS NULL THEN ? ELSE started_at END,
                       completed_at=CASE WHEN ? THEN ? ELSE NULL END,updated_at=?
                   WHERE id=? AND user_id=? AND actor_own_id=? AND status=? AND revision=?""",
                    (
                        status,
                        str(stage)[:80],
                        systemd_unit,
                        result_ref,
                        receipt_ref,
                        str(error_code)[:120] if error_code else None,
                        1 if reconciliation else 0,
                        status,
                        now,
                        1 if terminal else 0,
                        now,
                        now,
                        job_id,
                        user_id,
                        actor_own_id,
                        expected_status,
                        revision,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise HostJobTransitionError("host action status transition is invalid") from exc
            if cursor.rowcount != 1:
                raise HostJobTransitionError("host action transition lost its revision fence")
            conn.execute(
                """INSERT INTO host_action_events(
                       job_id,sequence,status,stage,outcome_code,receipt_digest,occurred_at)
                   SELECT ?,COALESCE(MAX(sequence),0)+1,?,?,?,?,?
                     FROM host_action_events WHERE job_id=?""",
                (
                    job_id,
                    status,
                    str(stage)[:80],
                    str(outcome_code)[:120],
                    receipt_digest,
                    now,
                    job_id,
                ),
            )
        row = self.get(job_id, user_id=user_id, actor_own_id=actor_own_id)
        if row is None:
            raise HostJobTransitionError("host action job disappeared")
        return row

    def list_reconcilable(
        self,
        *,
        host_agent_id: str,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        rows = self._storage.execute(
            """SELECT * FROM host_action_jobs
               WHERE host_agent_id=? AND reconciliation_required=1
                 AND status IN ('unknown','reconciling')
               ORDER BY updated_at,id LIMIT ?""",
            (str(host_agent_id), max(1, min(int(limit), 200))),
        ).fetchall()
        return [decoded for row in rows if (decoded := _decode_row(row)) is not None]

    def events(
        self,
        job_id: str,
        *,
        user_id: str,
        actor_own_id: str,
    ) -> list[dict[str, Any]]:
        if self.get(job_id, user_id=user_id, actor_own_id=actor_own_id) is None:
            return []
        return [
            dict(row)
            for row in self._storage.execute(
                "SELECT * FROM host_action_events WHERE job_id=? ORDER BY sequence",
                (job_id,),
            ).fetchall()
        ]


__all__ = ["HostJobConflict", "HostJobStore", "HostJobTransitionError"]
