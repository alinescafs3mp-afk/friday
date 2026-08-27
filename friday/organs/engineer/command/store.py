"""Transactional kernel ledger. Isolated from production host-agent job tables."""

from __future__ import annotations

import json
import os
import re
import secrets
import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any, Literal

from .contracts import CommandError, canonical_json_bytes

_LOCK_EX = 2
_LOCK_NB = 4
_LOCK_UN = 8
_JOB_ID = re.compile(r"[0-9a-f]{32}")
_KNOWN_JOB_STATUSES = frozenset(
    {"planned", "admitted", "running", "completed", "failed", "cancelled", "timeout", "unknown"}
)
_UNRESOLVED_JOB_STATUSES = frozenset({"planned", "admitted", "running", "unknown"})
_CANCELLABLE_JOB_STATUSES = frozenset({"planned", "admitted", "running"})


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


class CommandJobStore:
    """SQLite authority ledger plus per-job workspace directories."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)
        self._jobs = self.root / "jobs"
        self._jobs.mkdir(parents=True, exist_ok=True)
        os.chmod(self._jobs, 0o700)
        self.db_path = self.root / "kernel.sqlite"
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
        self.fail_next_commit = 0
        try:
            self._conn = sqlite3.connect(str(self.db_path), isolation_level=None, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=FULL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._init_schema()
        except Exception:
            os.close(self._lease_fd)
            os.close(self._lock_fd)
            raise

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

    def _init_schema(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS jobs (
                job_id TEXT PRIMARY KEY,
                actor_id TEXT NOT NULL,
                tenant_id TEXT NOT NULL,
                conversation_id TEXT NOT NULL,
                channel TEXT NOT NULL,
                source_row_id TEXT NOT NULL,
                source_hash TEXT NOT NULL,
                telegram_update_id TEXT NOT NULL,
                isolation_profile TEXT NOT NULL,
                host_user_authorized INTEGER NOT NULL,
                idempotency_key TEXT NOT NULL,
                command_digest TEXT NOT NULL,
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
                generated_files_json TEXT,
                executable_json TEXT,
                receipt_mac TEXT,
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
            CREATE INDEX IF NOT EXISTS idx_command_jobs_scope_status
                ON jobs(actor_id, tenant_id, conversation_id, channel, status, job_id);
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
        if "cleanup_pending" not in columns:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute(
                    "ALTER TABLE jobs ADD COLUMN cleanup_pending INTEGER NOT NULL DEFAULT 0"
                )
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

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        self._local.acquire()
        _flock(self._lock_fd, _LOCK_EX)
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
            else:
                if self.fail_next_commit > 0:
                    self.fail_next_commit -= 1
                    self._conn.execute("ROLLBACK")
                    raise CommandError("durable_write_failed")
                self._conn.execute("COMMIT")
        finally:
            _flock(self._lock_fd, _LOCK_UN)
            self._local.release()

    def job_dir(self, job_id: str) -> Path:
        if not job_id or "/" in job_id or job_id.startswith(".") or len(job_id) > 64:
            raise CommandError("invalid_job_id")
        return self._jobs / job_id

    def lookup_idempotency(self, actor_id: str, key: str) -> dict[str, str] | None:
        row = self._conn.execute(
            "SELECT job_id, command_digest FROM jobs WHERE actor_id=? AND idempotency_key=?",
            (actor_id, key),
        ).fetchone()
        if row is None:
            return None
        return {"job_id": str(row["job_id"]), "digest": str(row["command_digest"])}

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
        columns = (
            "job_id,actor_id,tenant_id,conversation_id,channel,source_row_id,source_hash,"
            "telegram_update_id,isolation_profile,host_user_authorized,idempotency_key,"
            "command_digest,argv_sha256,lane,origin,status,error_code,grant_nonce,"
            "timeout_sec,max_stdout_bytes,max_stderr_bytes,created_at,executable_json"
        )
        values = (
            payload["job_id"],
            payload["actor_id"],
            payload["tenant_id"],
            payload["conversation_id"],
            payload["channel"],
            payload["source_row_id"],
            payload["source_hash"],
            payload["telegram_update_id"],
            payload["isolation_profile"],
            1 if payload["host_user_authorized"] else 0,
            payload["idempotency_key"],
            payload["command_digest"],
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
            self._conn.execute(
                f"INSERT INTO jobs({columns}) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
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
        except sqlite3.IntegrityError as exc:
            raise CommandError("idempotency_conflict") from exc

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
        if job_id is not None and (
            not isinstance(job_id, str) or _JOB_ID.fullmatch(job_id) is None
        ):
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
