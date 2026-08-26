"""Independent HMAC trust domain and durable replay defence for the broker."""

from __future__ import annotations

import hashlib
import hmac
import sqlite3
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from friday.host_control.contracts import PROTOCOL_VERSION, RequestEnvelope, body_sha256

REQUEST_DOMAIN = b"friday-package-broker-request-v1"
RESPONSE_DOMAIN = b"friday-package-broker-response-v1"
RECEIPT_DOMAIN = b"friday-package-broker-receipt-v1"
RECONCILIATION_DOMAIN = b"friday-package-broker-reconciliation-v1"


class BrokerAuthenticationError(ValueError):
    """An authenticated request failed closed with one stable public code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class BrokerAuthenticator:
    """Create and verify request envelopes in the root-broker trust domain."""

    def __init__(
        self,
        key: bytes,
        *,
        broker_id: str,
        signing_private_key: bytes | None = None,
        verification_public_key: bytes | None = None,
        max_ttl_sec: int = 300,
        max_clock_skew_sec: int = 30,
    ) -> None:
        if not isinstance(key, bytes) or not 32 <= len(key) <= 64:
            raise ValueError("package-broker HMAC key must contain 32 to 64 bytes")
        if not broker_id:
            raise ValueError("package-broker identity is required")
        if not 1 <= max_ttl_sec <= 300 or not 0 <= max_clock_skew_sec <= 60:
            raise ValueError("package-broker authentication timing is invalid")
        self._key = bytes(key)
        if signing_private_key is None and verification_public_key is None:
            raise ValueError("package-broker response verification key is required")
        self._signing_key: Ed25519PrivateKey | None = None
        if signing_private_key is not None:
            if not isinstance(signing_private_key, bytes) or len(signing_private_key) != 32:
                raise ValueError("package-broker Ed25519 private key must contain 32 bytes")
            self._signing_key = Ed25519PrivateKey.from_private_bytes(signing_private_key)
            derived_public = self._signing_key.public_key()
        else:
            derived_public = None
        if verification_public_key is not None:
            if not isinstance(verification_public_key, bytes) or len(verification_public_key) != 32:
                raise ValueError("package-broker Ed25519 public key must contain 32 bytes")
            selected_public = Ed25519PublicKey.from_public_bytes(verification_public_key)
            if derived_public is not None and not hmac.compare_digest(
                verification_public_key,
                derived_public.public_bytes(
                    encoding=serialization.Encoding.Raw,
                    format=serialization.PublicFormat.Raw,
                ),
            ):
                raise ValueError("package-broker signing and verification keys do not match")
            self._verification_key = selected_public
        else:
            assert derived_public is not None
            self._verification_key = derived_public
        self.broker_id = broker_id
        self.max_ttl_sec = int(max_ttl_sec)
        self.max_clock_skew_sec = int(max_clock_skew_sec)

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
            agent_id=self.broker_id,
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
        return replace(unsigned, signature=self._request_signature(unsigned))

    def verify(
        self,
        envelope: RequestEnvelope,
        body: dict[str, Any],
        *,
        now: int | None = None,
    ) -> None:
        current = int(time.time()) if now is None else int(now)
        if envelope.agent_id != self.broker_id:
            raise BrokerAuthenticationError("broker_identity_mismatch")
        if envelope.protocol_version != PROTOCOL_VERSION:
            raise BrokerAuthenticationError("unsupported_protocol")
        if envelope.expires_at - envelope.issued_at > self.max_ttl_sec:
            raise BrokerAuthenticationError("invalid_expiry")
        if envelope.issued_at > current + self.max_clock_skew_sec:
            raise BrokerAuthenticationError("request_from_future")
        if envelope.expires_at <= current:
            raise BrokerAuthenticationError("request_expired")
        if not hmac.compare_digest(envelope.body_sha256, body_sha256(body)):
            raise BrokerAuthenticationError("body_hash_mismatch")
        expected = self._request_signature(replace(envelope, signature=""))
        if not hmac.compare_digest(envelope.signature, expected):
            raise BrokerAuthenticationError("invalid_signature")

    def sign_bytes(self, payload: bytes, *, domain: bytes) -> str:
        if not isinstance(payload, bytes) or not isinstance(domain, bytes) or not domain:
            raise ValueError("broker signature input is invalid")
        if self._signing_key is None:
            raise ValueError("package-broker private signing key is unavailable")
        return self._signing_key.sign(domain + b"\x00" + payload).hex()

    def verify_bytes(self, payload: bytes, signature: str, *, domain: bytes) -> bool:
        if not isinstance(signature, str) or len(signature) != 128:
            return False
        try:
            decoded = bytes.fromhex(signature)
            self._verification_key.verify(decoded, domain + b"\x00" + payload)
        except (InvalidSignature, ValueError):
            return False
        return True

    @property
    def public_key_bytes(self) -> bytes:
        return self._verification_key.public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )

    @property
    def can_sign_responses(self) -> bool:
        return self._signing_key is not None

    def _request_signature(self, envelope: RequestEnvelope) -> str:
        return hmac.new(
            self._key,
            REQUEST_DOMAIN + b"\x00" + envelope.signing_bytes(),
            hashlib.sha256,
        ).hexdigest()


class ReplayLedger:
    """One-time request and sequence admission that survives broker restarts."""

    def __init__(self, database: str | Path, *, allow_memory: bool = False) -> None:
        selected = str(database)
        if selected == ":memory:" and not allow_memory:
            raise ValueError("in-memory replay state requires explicit test opt-in")
        self._lock = threading.Lock()
        self._connection = sqlite3.connect(selected, check_same_thread=False, isolation_level=None)
        self._connection.execute("PRAGMA busy_timeout=5000")
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA synchronous=FULL")
        self._connection.execute(
            """CREATE TABLE IF NOT EXISTS admitted_broker_requests (
                   request_id TEXT PRIMARY KEY,
                   broker_id TEXT NOT NULL,
                   sequence INTEGER NOT NULL,
                   expires_at INTEGER NOT NULL,
                   UNIQUE(broker_id, sequence)
               )"""
        )

    def admit(self, envelope: RequestEnvelope, *, now: int | None = None) -> None:
        current = int(time.time()) if now is None else int(now)
        if envelope.expires_at <= current:
            raise BrokerAuthenticationError("request_expired")
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                self._connection.execute(
                    "DELETE FROM admitted_broker_requests WHERE expires_at <= ?", (current,)
                )
                self._connection.execute(
                    """INSERT INTO admitted_broker_requests
                           (request_id, broker_id, sequence, expires_at)
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
                raise BrokerAuthenticationError("replayed_request") from exc
            except BaseException:
                self._connection.execute("ROLLBACK")
                raise
            else:
                self._connection.execute("COMMIT")

    def close(self) -> None:
        with self._lock:
            self._connection.close()


__all__ = [
    "RECEIPT_DOMAIN",
    "RECONCILIATION_DOMAIN",
    "REQUEST_DOMAIN",
    "RESPONSE_DOMAIN",
    "BrokerAuthenticationError",
    "BrokerAuthenticator",
    "ReplayLedger",
]
