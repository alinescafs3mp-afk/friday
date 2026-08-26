"""Transactional kernel ledger. Isolated from production host-agent job tables."""

from __future__ import annotations

import json
import os
import secrets
import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any

from .contracts import CommandError, canonical_json_bytes

_LOCK_EX = 2
_LOCK_UN = 8


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
        if self.db_path.is_symlink() or (self.root / "kernel.lock").is_symlink():
            raise CommandError("durable_write_failed")
        lock_path = self.root / "kernel.lock"
        lock_flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            self._lock_fd = os.open(str(lock_path), lock_flags, 0o600)
        except OSError as exc:
            raise CommandError("durable_write_failed") from exc
        os.fchmod(self._lock_fd, 0o600)
        self._local = threading.RLock()
        self.fail_next_commit = 0
        self._conn = sqlite3.connect(str(self.db_path), isolation_level=None, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._init_schema()

    def close(self) -> None:
        with self._local:
            self._conn.close()
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
            CREATE TABLE IF NOT EXISTS grant_nonces (
                nonce TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                exp INTEGER NOT NULL
            );
            """
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
        except sqlite3.IntegrityError as exc:
            raise CommandError("idempotency_conflict") from exc

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
                "SELECT * FROM jobs WHERE status IN ('admitted','running')"
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
