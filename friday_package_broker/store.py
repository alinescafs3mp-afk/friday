"""Durable, idempotent package-plan state and content-free broker audit."""

from __future__ import annotations

import os
import re
import sqlite3
import threading
import time
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from .contracts import (
    BROKER_RECEIPT_SCHEMA_VERSION,
    AptInstallPlan,
    PackageReconciliationReceipt,
    PackageTransactionReceipt,
)

_TRANSACTION_ID = re.compile(r"^apttxn_[0-9a-f]{16,64}$")
_APPROVAL_PROOF_ID = re.compile(r"^approvalproof_[0-9a-f]{32,64}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")


class PlanStatus(StrEnum):
    PLANNED = "planned"
    EXECUTING = "executing"
    COMPLETED = "completed"
    CANCELLED_BEFORE_COMMIT = "cancelled_before_commit"
    FAILED_BEFORE_EFFECT = "failed_before_effect"
    UNKNOWN = "unknown"


class BrokerStoreError(RuntimeError):
    code = "broker_store_error"


class StoreNotFound(BrokerStoreError):
    code = "plan_not_found"


class StoreConflict(BrokerStoreError):
    code = "idempotency_conflict"


class StoreStateError(BrokerStoreError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class StoredPlan:
    plan: AptInstallPlan
    request_digest: str
    plan_idempotency_key: str
    status: PlanStatus
    execution_idempotency_key: str | None
    transaction_id: str | None
    execution_started_at: int | None
    approval_receipt_id: str | None
    approval_proof_id: str | None
    approval_proof_digest: str | None
    receipt: PackageTransactionReceipt | None
    reconciliation: PackageReconciliationReceipt | None
    reconciliation_idempotency_key: str | None
    error_code: str | None
    updated_at: int


@dataclass(frozen=True, slots=True)
class ExecutionClaim:
    record: StoredPlan
    should_execute: bool


class BrokerStore:
    """SQLite state machine; an interrupted execution is never auto-retried."""

    def __init__(self, database: str | Path, *, allow_memory: bool = False) -> None:
        selected = str(database)
        if selected == ":memory:" and not allow_memory:
            raise ValueError("in-memory broker state requires explicit test opt-in")
        self._database = selected
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(selected, check_same_thread=False, isolation_level=None)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA busy_timeout=5000")
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA synchronous=FULL")
        self._connection.execute("PRAGMA foreign_keys=ON")
        self._create_schema()
        if selected != ":memory:":
            os.chmod(selected, 0o600)
        self._recover_interrupted()

    def _create_schema(self) -> None:
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS broker_plans (
                plan_id TEXT PRIMARY KEY,
                actor_user_id TEXT NOT NULL,
                actor_own_id TEXT NOT NULL,
                plan_idempotency_key TEXT NOT NULL UNIQUE,
                request_digest TEXT NOT NULL,
                plan_json BLOB NOT NULL,
                plan_digest TEXT NOT NULL,
                transaction_digest TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN (
                    'planned', 'executing', 'completed', 'cancelled_before_commit',
                    'failed_before_effect', 'unknown'
                )),
                execution_idempotency_key TEXT UNIQUE,
                transaction_id TEXT UNIQUE,
                execution_started_at INTEGER,
                approval_receipt_id TEXT,
                approval_proof_id TEXT UNIQUE,
                approval_proof_digest TEXT,
                receipt_json BLOB,
                reconciliation_json BLOB,
                reconciliation_idempotency_key TEXT,
                error_code TEXT,
                created_at INTEGER NOT NULL,
                expires_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS broker_events (
                event_sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                plan_id TEXT NOT NULL,
                transaction_id TEXT,
                event_code TEXT NOT NULL CHECK(length(event_code) BETWEEN 1 AND 80),
                observed_at INTEGER NOT NULL,
                FOREIGN KEY(plan_id) REFERENCES broker_plans(plan_id)
            );
            CREATE INDEX IF NOT EXISTS ix_broker_events_plan
                ON broker_events(plan_id, event_sequence);
            CREATE TABLE IF NOT EXISTS consumed_package_approvals (
                proof_id TEXT PRIMARY KEY,
                proof_digest TEXT NOT NULL,
                plan_id TEXT NOT NULL UNIQUE,
                consumed_at INTEGER NOT NULL,
                FOREIGN KEY(plan_id) REFERENCES broker_plans(plan_id)
            );
            """
        )
        columns = {
            str(row["name"]) for row in self._connection.execute("PRAGMA table_info(broker_plans)").fetchall()
        }
        if "approval_proof_id" not in columns:
            self._connection.execute("ALTER TABLE broker_plans ADD COLUMN approval_proof_id TEXT")
            self._connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS ux_broker_plans_approval_proof_id "
                "ON broker_plans(approval_proof_id)"
            )
        if "approval_proof_digest" not in columns:
            self._connection.execute("ALTER TABLE broker_plans ADD COLUMN approval_proof_digest TEXT")
        if "reconciliation_json" not in columns:
            self._connection.execute("ALTER TABLE broker_plans ADD COLUMN reconciliation_json BLOB")
        if "reconciliation_idempotency_key" not in columns:
            self._connection.execute(
                "ALTER TABLE broker_plans ADD COLUMN reconciliation_idempotency_key TEXT"
            )

    def _recover_interrupted(self) -> None:
        current = int(time.time())
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                rows = self._connection.execute(
                    "SELECT plan_id, transaction_id FROM broker_plans WHERE status = 'executing'"
                ).fetchall()
                for row in rows:
                    self._connection.execute(
                        """UPDATE broker_plans
                           SET status = 'unknown',
                               error_code = 'broker_restart_after_effect_claim',
                               updated_at = ?
                           WHERE plan_id = ?""",
                        (current, row["plan_id"]),
                    )
                    self._event(
                        row["plan_id"],
                        "execution_recovered_unknown",
                        current,
                        transaction_id=row["transaction_id"],
                    )
            except BaseException:
                self._connection.execute("ROLLBACK")
                raise
            else:
                self._connection.execute("COMMIT")

    def save_plan(
        self,
        plan: AptInstallPlan,
        *,
        request_digest: str,
        idempotency_key: str,
    ) -> StoredPlan:
        payload = plan.canonical_bytes()
        current = int(time.time())
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                existing = self._connection.execute(
                    "SELECT * FROM broker_plans WHERE plan_idempotency_key = ?",
                    (idempotency_key,),
                ).fetchone()
                if existing is not None:
                    record = self._record(existing)
                    self._assert_exact_plan_retry(
                        record,
                        request_digest=request_digest,
                        actor_user_id=plan.actor_user_id,
                        actor_own_id=plan.actor_own_id,
                    )
                    self._connection.execute("COMMIT")
                    return record
                self._connection.execute(
                    """INSERT INTO broker_plans (
                           plan_id, actor_user_id, actor_own_id, plan_idempotency_key,
                           request_digest, plan_json, plan_digest, transaction_digest,
                           status, created_at, expires_at, updated_at
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'planned', ?, ?, ?)""",
                    (
                        plan.plan_id,
                        plan.actor_user_id,
                        plan.actor_own_id,
                        idempotency_key,
                        request_digest,
                        payload,
                        plan.digest,
                        plan.transaction.digest,
                        plan.created_at,
                        plan.expires_at,
                        current,
                    ),
                )
                self._event(plan.plan_id, "plan_created", current, transaction_id=None)
                row = self._connection.execute(
                    "SELECT * FROM broker_plans WHERE plan_id = ?", (plan.plan_id,)
                ).fetchone()
                assert row is not None
                record = self._record(row)
            except sqlite3.IntegrityError as exc:
                self._connection.execute("ROLLBACK")
                raise StoreConflict("idempotency_conflict") from exc
            except BaseException:
                self._connection.execute("ROLLBACK")
                raise
            else:
                self._connection.execute("COMMIT")
                return record

    def idempotent_plan(
        self,
        *,
        idempotency_key: str,
        request_digest: str,
        actor_user_id: str,
        actor_own_id: str,
    ) -> StoredPlan | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM broker_plans WHERE plan_idempotency_key = ?", (idempotency_key,)
            ).fetchone()
            if row is None:
                return None
            record = self._record(row)
            self._assert_exact_plan_retry(
                record,
                request_digest=request_digest,
                actor_user_id=actor_user_id,
                actor_own_id=actor_own_id,
            )
            return record

    def get(self, plan_id: str, *, actor_user_id: str, actor_own_id: str) -> StoredPlan:
        with self._lock:
            row = self._connection.execute(
                """SELECT * FROM broker_plans
                   WHERE plan_id = ? AND actor_user_id = ? AND actor_own_id = ?""",
                (plan_id, actor_user_id, actor_own_id),
            ).fetchone()
            if row is None:
                raise StoreNotFound("plan_not_found")
            return self._record(row)

    def claim_execution(
        self,
        plan_id: str,
        *,
        actor_user_id: str,
        actor_own_id: str,
        plan_digest: str,
        execution_idempotency_key: str,
        transaction_id: str,
        approval_receipt_id: str,
        approval_proof_id: str,
        approval_proof_digest: str,
        now: int,
    ) -> ExecutionClaim:
        if not isinstance(transaction_id, str) or _TRANSACTION_ID.fullmatch(transaction_id) is None:
            raise StoreStateError("invalid_transaction_identity")
        if (
            not isinstance(approval_proof_id, str)
            or _APPROVAL_PROOF_ID.fullmatch(approval_proof_id) is None
            or not isinstance(approval_proof_digest, str)
            or _DIGEST.fullmatch(approval_proof_digest) is None
        ):
            raise StoreStateError("invalid_approval_proof_identity")
        if isinstance(now, bool) or not isinstance(now, int):
            raise StoreStateError("invalid_execution_time")
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                row = self._scoped_row(plan_id, actor_user_id, actor_own_id)
                record = self._record(row)
                if record.plan.digest != plan_digest:
                    raise StoreStateError("approved_plan_mismatch")
                if record.execution_idempotency_key is not None:
                    if (
                        record.execution_idempotency_key != execution_idempotency_key
                        or record.approval_receipt_id != approval_receipt_id
                        or record.approval_proof_id != approval_proof_id
                        or record.approval_proof_digest != approval_proof_digest
                    ):
                        raise StoreConflict("execution_idempotency_conflict")
                    self._connection.execute("COMMIT")
                    return ExecutionClaim(record=record, should_execute=False)
                if record.status is not PlanStatus.PLANNED:
                    raise StoreStateError("plan_not_executable")
                if record.plan.expires_at <= now:
                    self._connection.execute(
                        """UPDATE broker_plans
                           SET status = 'failed_before_effect', error_code = 'plan_expired', updated_at = ?
                           WHERE plan_id = ?""",
                        (now, plan_id),
                    )
                    self._event(plan_id, "plan_expired", now, transaction_id=None)
                    expired = self._record(
                        self._connection.execute(
                            "SELECT * FROM broker_plans WHERE plan_id = ?", (plan_id,)
                        ).fetchone()
                    )
                    self._connection.execute("COMMIT")
                    return ExecutionClaim(record=expired, should_execute=False)
                try:
                    self._connection.execute(
                        """INSERT INTO consumed_package_approvals (
                               proof_id, proof_digest, plan_id, consumed_at
                           ) VALUES (?, ?, ?, ?)""",
                        (approval_proof_id, approval_proof_digest, plan_id, now),
                    )
                except sqlite3.IntegrityError as exc:
                    raise StoreStateError("replayed_approval") from exc
                self._connection.execute(
                    """UPDATE broker_plans
                       SET status = 'executing', execution_idempotency_key = ?,
                           transaction_id = ?, execution_started_at = ?,
                           approval_receipt_id = ?, approval_proof_id = ?,
                           approval_proof_digest = ?, error_code = NULL, updated_at = ?
                       WHERE plan_id = ? AND status = 'planned'""",
                    (
                        execution_idempotency_key,
                        transaction_id,
                        now,
                        approval_receipt_id,
                        approval_proof_id,
                        approval_proof_digest,
                        now,
                        plan_id,
                    ),
                )
                self._event(plan_id, "execution_claimed", now, transaction_id=transaction_id)
                claimed = self._record(
                    self._connection.execute(
                        "SELECT * FROM broker_plans WHERE plan_id = ?", (plan_id,)
                    ).fetchone()
                )
            except (StoreConflict, StoreNotFound, StoreStateError):
                self._connection.execute("ROLLBACK")
                raise
            except sqlite3.IntegrityError as exc:
                self._connection.execute("ROLLBACK")
                raise StoreConflict("execution_idempotency_conflict") from exc
            except BaseException:
                self._connection.execute("ROLLBACK")
                raise
            else:
                self._connection.execute("COMMIT")
                return ExecutionClaim(record=claimed, should_execute=True)

    def finish_execution(
        self,
        plan_id: str,
        *,
        receipt: PackageTransactionReceipt,
        status: PlanStatus,
        error_code: str | None,
        now: int,
    ) -> StoredPlan:
        if receipt.schema_version != BROKER_RECEIPT_SCHEMA_VERSION:
            raise ValueError("new package effects require the current receipt schema")
        if status not in {
            PlanStatus.COMPLETED,
            PlanStatus.FAILED_BEFORE_EFFECT,
            PlanStatus.UNKNOWN,
        }:
            raise ValueError("terminal package execution status is invalid")
        payload = receipt.canonical_bytes()
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                row = self._connection.execute(
                    "SELECT * FROM broker_plans WHERE plan_id = ?", (plan_id,)
                ).fetchone()
                if row is None:
                    raise StoreNotFound("plan_not_found")
                record = self._record(row)
                if record.status is not PlanStatus.EXECUTING:
                    raise StoreStateError("execution_not_claimed")
                if record.transaction_id != receipt.transaction_id:
                    raise StoreStateError("transaction_identity_mismatch")
                self._connection.execute(
                    """UPDATE broker_plans
                       SET status = ?, receipt_json = ?, error_code = ?, updated_at = ?
                       WHERE plan_id = ? AND status = 'executing'""",
                    (status.value, payload, error_code, now, plan_id),
                )
                self._event(
                    plan_id,
                    f"execution_{status.value}",
                    now,
                    transaction_id=receipt.transaction_id,
                )
                finished = self._record(
                    self._connection.execute(
                        "SELECT * FROM broker_plans WHERE plan_id = ?", (plan_id,)
                    ).fetchone()
                )
            except (StoreNotFound, StoreStateError):
                self._connection.execute("ROLLBACK")
                raise
            except BaseException:
                self._connection.execute("ROLLBACK")
                raise
            else:
                self._connection.execute("COMMIT")
                return finished

    def idempotent_reconciliation(
        self,
        plan_id: str,
        *,
        actor_user_id: str,
        actor_own_id: str,
        plan_digest: str,
        idempotency_key: str,
    ) -> StoredPlan | None:
        with self._lock:
            record = self._record(self._scoped_row(plan_id, actor_user_id, actor_own_id))
            if record.plan.digest != plan_digest:
                raise StoreStateError("approved_plan_mismatch")
            if record.reconciliation_idempotency_key is None:
                return None
            if record.reconciliation_idempotency_key != idempotency_key:
                raise StoreConflict("reconciliation_idempotency_conflict")
            if record.reconciliation is None:
                raise BrokerStoreError("stored package reconciliation is incomplete")
            return record

    def save_reconciliation(
        self,
        plan_id: str,
        *,
        actor_user_id: str,
        actor_own_id: str,
        plan_digest: str,
        idempotency_key: str,
        receipt: PackageReconciliationReceipt,
        now: int,
    ) -> StoredPlan:
        payload = receipt.canonical_bytes()
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                record = self._record(self._scoped_row(plan_id, actor_user_id, actor_own_id))
                if record.plan.digest != plan_digest:
                    raise StoreStateError("approved_plan_mismatch")
                if (
                    record.status is not PlanStatus.UNKNOWN
                    or record.error_code != "broker_restart_after_effect_claim"
                    or record.receipt is not None
                    or record.transaction_id is None
                    or record.approval_receipt_id is None
                ):
                    raise StoreStateError("reconciliation_not_allowed")
                if record.reconciliation_idempotency_key is not None:
                    if record.reconciliation_idempotency_key != idempotency_key:
                        raise StoreConflict("reconciliation_idempotency_conflict")
                    if record.reconciliation != receipt:
                        raise StoreConflict("reconciliation_result_conflict")
                    self._connection.execute("COMMIT")
                    return record
                if (
                    receipt.plan_id != record.plan.plan_id
                    or receipt.plan_digest != record.plan.digest
                    or receipt.transaction_digest != record.plan.transaction.digest
                    or receipt.transaction_id != record.transaction_id
                    or receipt.approval_receipt_id != record.approval_receipt_id
                    or receipt.actor_user_id != record.plan.actor_user_id
                    or receipt.actor_own_id != record.plan.actor_own_id
                    or receipt.continuation_work_item_id != record.plan.continuation_work_item_id
                    or receipt.reconciliation_idempotency_key != idempotency_key
                ):
                    raise StoreStateError("reconciliation_binding_mismatch")
                self._connection.execute(
                    """UPDATE broker_plans
                       SET reconciliation_json = ?, reconciliation_idempotency_key = ?, updated_at = ?
                       WHERE plan_id = ? AND status = 'unknown'""",
                    (payload, idempotency_key, now, plan_id),
                )
                self._event(
                    plan_id,
                    f"reconciliation_{receipt.postcondition_state.value}",
                    now,
                    transaction_id=receipt.transaction_id,
                )
                saved = self._record(
                    self._connection.execute(
                        "SELECT * FROM broker_plans WHERE plan_id = ?", (plan_id,)
                    ).fetchone()
                )
            except (StoreConflict, StoreNotFound, StoreStateError):
                self._connection.execute("ROLLBACK")
                raise
            except BaseException:
                self._connection.execute("ROLLBACK")
                raise
            else:
                self._connection.execute("COMMIT")
                return saved

    def cancel_before_commit(
        self,
        plan_id: str,
        *,
        actor_user_id: str,
        actor_own_id: str,
        now: int,
    ) -> StoredPlan:
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                record = self._record(self._scoped_row(plan_id, actor_user_id, actor_own_id))
                if record.status is PlanStatus.CANCELLED_BEFORE_COMMIT:
                    self._connection.execute("COMMIT")
                    return record
                if record.status is not PlanStatus.PLANNED:
                    raise StoreStateError("too_late_to_cancel")
                self._connection.execute(
                    """UPDATE broker_plans
                       SET status = 'cancelled_before_commit', error_code = NULL, updated_at = ?
                       WHERE plan_id = ? AND status = 'planned'""",
                    (now, plan_id),
                )
                self._event(plan_id, "cancelled_before_commit", now, transaction_id=None)
                cancelled = self._record(
                    self._connection.execute(
                        "SELECT * FROM broker_plans WHERE plan_id = ?", (plan_id,)
                    ).fetchone()
                )
            except (StoreNotFound, StoreStateError):
                self._connection.execute("ROLLBACK")
                raise
            except BaseException:
                self._connection.execute("ROLLBACK")
                raise
            else:
                self._connection.execute("COMMIT")
                return cancelled

    def _scoped_row(self, plan_id: str, actor_user_id: str, actor_own_id: str) -> sqlite3.Row:
        row = self._connection.execute(
            """SELECT * FROM broker_plans
               WHERE plan_id = ? AND actor_user_id = ? AND actor_own_id = ?""",
            (plan_id, actor_user_id, actor_own_id),
        ).fetchone()
        if row is None:
            raise StoreNotFound("plan_not_found")
        return row

    @staticmethod
    def _assert_exact_plan_retry(
        record: StoredPlan,
        *,
        request_digest: str,
        actor_user_id: str,
        actor_own_id: str,
    ) -> None:
        if (
            record.request_digest != request_digest
            or record.plan.actor_user_id != actor_user_id
            or record.plan.actor_own_id != actor_own_id
        ):
            raise StoreConflict("idempotency_conflict")

    @staticmethod
    def _record(row: sqlite3.Row) -> StoredPlan:
        plan = AptInstallPlan.from_canonical_bytes(bytes(row["plan_json"]))
        receipt_payload = row["receipt_json"]
        receipt = None
        if receipt_payload is not None:
            from friday.host_control.contracts import decode_canonical_json

            receipt = PackageTransactionReceipt.from_payload(
                decode_canonical_json(bytes(receipt_payload), maximum=256 * 1024)
            )
        reconciliation_payload = row["reconciliation_json"]
        reconciliation = None
        if reconciliation_payload is not None:
            from friday.host_control.contracts import decode_canonical_json

            reconciliation = PackageReconciliationReceipt.from_payload(
                decode_canonical_json(bytes(reconciliation_payload), maximum=256 * 1024)
            )
        if plan.digest != row["plan_digest"] or plan.transaction.digest != row["transaction_digest"]:
            raise BrokerStoreError("stored package plan digest mismatch")
        execution_values = (
            row["execution_idempotency_key"],
            row["transaction_id"],
            row["execution_started_at"],
            row["approval_receipt_id"],
        )
        if any(value is None for value in execution_values) != all(
            value is None for value in execution_values
        ):
            raise BrokerStoreError("stored package execution metadata is incomplete")
        proof_values = (row["approval_proof_id"], row["approval_proof_digest"])
        if (proof_values[0] is None) != (proof_values[1] is None) or (
            all(value is None for value in execution_values) and proof_values[0] is not None
        ):
            raise BrokerStoreError("stored package approval proof metadata is incomplete")
        if row["transaction_id"] is not None and _TRANSACTION_ID.fullmatch(row["transaction_id"]) is None:
            raise BrokerStoreError("stored package transaction identity is invalid")
        if (row["reconciliation_idempotency_key"] is None) != (reconciliation is None):
            raise BrokerStoreError("stored package reconciliation metadata is incomplete")
        return StoredPlan(
            plan=plan,
            request_digest=row["request_digest"],
            plan_idempotency_key=row["plan_idempotency_key"],
            status=PlanStatus(row["status"]),
            execution_idempotency_key=row["execution_idempotency_key"],
            transaction_id=row["transaction_id"],
            execution_started_at=row["execution_started_at"],
            approval_receipt_id=row["approval_receipt_id"],
            approval_proof_id=row["approval_proof_id"],
            approval_proof_digest=row["approval_proof_digest"],
            receipt=receipt,
            reconciliation=reconciliation,
            reconciliation_idempotency_key=row["reconciliation_idempotency_key"],
            error_code=row["error_code"],
            updated_at=row["updated_at"],
        )

    def _event(
        self,
        plan_id: str,
        event_code: str,
        observed_at: int,
        *,
        transaction_id: str | None,
    ) -> None:
        self._connection.execute(
            """INSERT INTO broker_events(plan_id, transaction_id, event_code, observed_at)
               VALUES (?, ?, ?, ?)""",
            (plan_id, transaction_id, event_code, observed_at),
        )

    def close(self) -> None:
        with self._lock:
            self._connection.close()


__all__ = [
    "BrokerStore",
    "BrokerStoreError",
    "ExecutionClaim",
    "PlanStatus",
    "StoreConflict",
    "StoreNotFound",
    "StoreStateError",
    "StoredPlan",
]
