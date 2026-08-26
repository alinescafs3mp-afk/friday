"""HMAC request authentication and a durable replay ledger."""

from __future__ import annotations

import hashlib
import hmac
import os
import sqlite3
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

from .protocol import PROTOCOL_VERSION, ProtocolError, RequestEnvelope, body_sha256


class HMACAuthenticator:
    def __init__(
        self,
        key: bytes,
        *,
        agent_id: str,
        max_ttl_sec: int = 300,
        max_clock_skew_sec: int = 30,
    ) -> None:
        if len(key) < 32:
            raise ValueError("host-agent HMAC key must contain at least 32 bytes")
        self._key = bytes(key)
        self.agent_id = agent_id
        self.max_ttl_sec = max(1, int(max_ttl_sec))
        self.max_clock_skew_sec = max(0, int(max_clock_skew_sec))

    def create_envelope(
        self,
        *,
        request_id: str,
        sequence: int,
        issued_at: int,
        expires_at: int,
        method: str,
        job_id: str,
        actor_id: str,
        own_id: str,
        idempotency_key: str,
        plan_digest: str,
        body: dict[str, Any],
        approval_receipt_id: str | None = None,
    ) -> RequestEnvelope:
        unsigned = RequestEnvelope(
            protocol_version=PROTOCOL_VERSION,
            request_id=request_id,
            agent_id=self.agent_id,
            sequence=sequence,
            issued_at=issued_at,
            expires_at=expires_at,
            method=method,
            job_id=job_id,
            actor_id=actor_id,
            own_id=own_id,
            idempotency_key=idempotency_key,
            plan_digest=plan_digest,
            approval_receipt_id=approval_receipt_id,
            body_sha256=body_sha256(body),
            signature="",
        )
        return replace(unsigned, signature=self._signature(unsigned))

    def verify(
        self,
        envelope: RequestEnvelope,
        body: dict[str, Any],
        *,
        now: int | None = None,
    ) -> None:
        current = int(time.time()) if now is None else int(now)
        if envelope.agent_id != self.agent_id:
            raise ProtocolError("agent_identity_mismatch", "request names a different host agent")
        if envelope.expires_at - envelope.issued_at > self.max_ttl_sec:
            raise ProtocolError("invalid_expiry", "request lifetime exceeds the configured maximum")
        if envelope.issued_at > current + self.max_clock_skew_sec:
            raise ProtocolError("request_from_future", "request issue time is beyond clock tolerance")
        if envelope.expires_at <= current:
            raise ProtocolError("request_expired", "request has expired")
        if not hmac.compare_digest(envelope.body_sha256, body_sha256(body)):
            raise ProtocolError("body_hash_mismatch", "request body does not match its signed digest")
        expected = self._signature(replace(envelope, signature=""))
        if not hmac.compare_digest(envelope.signature, expected):
            raise ProtocolError("invalid_signature", "request signature is invalid")

    def sign_bytes(self, payload: bytes, *, domain: bytes) -> str:
        return hmac.new(self._key, domain + b"\x00" + payload, hashlib.sha256).hexdigest()

    def verify_bytes(self, payload: bytes, signature: str, *, domain: bytes) -> bool:
        return hmac.compare_digest(signature, self.sign_bytes(payload, domain=domain))

    def _signature(self, envelope: RequestEnvelope) -> str:
        return self.sign_bytes(envelope.signing_bytes(), domain=b"friday-host-agent-request-v1")


class ReplayGuard:
    """SQLite-backed one-time request/sequence admission that survives restarts."""

    def __init__(self, database: str | Path = ":memory:") -> None:
        self._lock = threading.Lock()
        database_value = str(database)
        persistent_path: Path | None = None
        if database_value != ":memory:":
            persistent_path = Path(database_value)
            if (
                not persistent_path.is_absolute()
                or persistent_path.is_symlink()
                or persistent_path.parent.is_symlink()
                or str(persistent_path.parent.resolve(strict=True)) != str(persistent_path.parent)
            ):
                raise ValueError("replay database path must be canonical and absolute")
            if persistent_path.exists() and persistent_path.stat().st_uid != os.geteuid():
                raise ValueError("replay database is owned by another account")
        self._connection = sqlite3.connect(database_value, check_same_thread=False, isolation_level=None)
        if persistent_path is not None:
            os.chmod(persistent_path, 0o600)
        self._connection.execute("PRAGMA busy_timeout=5000")
        self._connection.execute(
            """CREATE TABLE IF NOT EXISTS admitted_requests (
                   request_id TEXT PRIMARY KEY,
                   agent_id TEXT NOT NULL,
                   sequence INTEGER NOT NULL,
                   expires_at INTEGER NOT NULL,
                   UNIQUE(agent_id, sequence)
               )"""
        )

    def admit(self, envelope: RequestEnvelope, *, now: int | None = None) -> None:
        current = int(time.time()) if now is None else int(now)
        if envelope.expires_at <= current:
            raise ProtocolError("request_expired", "request has expired")
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                self._connection.execute("DELETE FROM admitted_requests WHERE expires_at < ?", (current,))
                self._connection.execute(
                    """INSERT INTO admitted_requests(request_id, agent_id, sequence, expires_at)
                       VALUES (?, ?, ?, ?)""",
                    (
                        envelope.request_id,
                        envelope.agent_id,
                        envelope.sequence,
                        envelope.expires_at,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                self._connection.execute("ROLLBACK")
                raise ProtocolError(
                    "replayed_request", "request id or sequence was already admitted"
                ) from exc
            else:
                self._connection.execute("COMMIT")

    def close(self) -> None:
        self._connection.close()


__all__ = ["HMACAuthenticator", "ReplayGuard"]
