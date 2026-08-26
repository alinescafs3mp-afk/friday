"""Exact, expiring approvals for public-network host actions."""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import sqlite3
import stat
import threading
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from friday_package_broker.approval import load_broker_approval_public_key

from .contracts import PROTOCOL_VERSION, ContractError, canonical_json_bytes

NETWORK_APPROVAL_DOMAIN = b"friday-public-network-action-approval-v1"
NETWORK_APPROVAL_SCHEMA_VERSION = 1
MAX_NETWORK_APPROVAL_TTL_SEC = 300

_ACTOR_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@+\-]{0,199}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_PROOF_ID = re.compile(r"^networkapproval_[0-9a-f]{32,64}$")
_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:\-]{0,199}$")
_SIGNATURE = re.compile(r"^[0-9a-f]{128}$")


class NetworkApprovalError(ContractError):
    """A public-network approval is malformed, untrusted, expired, or reused."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class NetworkApprovalProof:
    """One authorization for one exact public-network action admission."""

    schema_version: int
    protocol_version: str
    proof_id: str
    host_agent_id: str
    approval_receipt_id: str
    approval_payload_digest: str
    plan_id: str
    plan_digest: str
    job_id: str
    execution_idempotency_key: str
    actor_user_id: str
    actor_own_id: str
    issued_at: int
    expires_at: int
    signature: str = ""

    def __post_init__(self) -> None:
        if (
            self.schema_version != NETWORK_APPROVAL_SCHEMA_VERSION
            or self.protocol_version != PROTOCOL_VERSION
        ):
            raise NetworkApprovalError("network_approval_version_invalid")
        if _PROOF_ID.fullmatch(self.proof_id) is None:
            raise NetworkApprovalError("network_approval_identity_invalid")
        for value, field in (
            (self.host_agent_id, "agent"),
            (self.approval_receipt_id, "receipt"),
            (self.plan_id, "plan"),
            (self.job_id, "job"),
            (self.execution_idempotency_key, "idempotency"),
        ):
            if not isinstance(value, str) or _REF.fullmatch(value) is None:
                raise NetworkApprovalError(f"network_approval_{field}_invalid")
        for value, field in (
            (self.actor_user_id, "actor"),
            (self.actor_own_id, "owner"),
        ):
            if not isinstance(value, str) or _ACTOR_ID.fullmatch(value) is None:
                raise NetworkApprovalError(f"network_approval_{field}_invalid")
        if _DIGEST.fullmatch(self.approval_payload_digest) is None:
            raise NetworkApprovalError("network_approval_payload_digest_invalid")
        if _DIGEST.fullmatch(self.plan_digest) is None:
            raise NetworkApprovalError("network_approval_plan_digest_invalid")
        if any(
            isinstance(value, bool) or not isinstance(value, int)
            for value in (self.issued_at, self.expires_at)
        ):
            raise NetworkApprovalError("network_approval_time_invalid")
        if not self.issued_at < self.expires_at <= self.issued_at + MAX_NETWORK_APPROVAL_TTL_SEC:
            raise NetworkApprovalError("network_approval_expiry_invalid")
        if self.signature and _SIGNATURE.fullmatch(self.signature) is None:
            raise NetworkApprovalError("network_approval_signature_invalid")

    def unsigned_payload(self) -> dict[str, Any]:
        payload = asdict(self)
        payload.pop("signature")
        return payload

    def canonical_bytes_for_signing(self) -> bytes:
        return canonical_json_bytes(self.unsigned_payload(), maximum=16 * 1024)

    def to_payload(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def digest(self) -> str:
        return hashlib.sha256(canonical_json_bytes(self.to_payload(), maximum=16 * 1024)).hexdigest()

    def with_signature(self, signature: str) -> NetworkApprovalProof:
        return replace(self, signature=signature)

    @classmethod
    def from_payload(cls, value: Any) -> NetworkApprovalProof:
        if not isinstance(value, dict) or set(value) != set(cls.__dataclass_fields__):
            raise NetworkApprovalError("network_approval_fields_invalid")
        try:
            return cls(**value)
        except TypeError as exc:
            raise NetworkApprovalError("network_approval_fields_invalid") from exc


class NetworkApprovalSigner:
    """Domain-separated signer held by the backend authorization boundary."""

    def __init__(self, private_key: bytes) -> None:
        if not isinstance(private_key, bytes) or len(private_key) != 32:
            raise ValueError("network approval Ed25519 private key must contain 32 bytes")
        self._key = Ed25519PrivateKey.from_private_bytes(private_key)

    @property
    def public_key_bytes(self) -> bytes:
        return self._key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )

    @property
    def public_key_digest(self) -> str:
        return hashlib.sha256(self.public_key_bytes).hexdigest()

    def issue(
        self,
        *,
        host_agent_id: str,
        approval_receipt_id: str,
        approval_payload_digest: str,
        plan_id: str,
        plan_digest: str,
        job_id: str,
        execution_idempotency_key: str,
        actor_user_id: str,
        actor_own_id: str,
        issued_at: int,
        expires_at: int,
    ) -> NetworkApprovalProof:
        unsigned = NetworkApprovalProof(
            schema_version=NETWORK_APPROVAL_SCHEMA_VERSION,
            protocol_version=PROTOCOL_VERSION,
            proof_id=f"networkapproval_{secrets.token_hex(16)}",
            host_agent_id=host_agent_id,
            approval_receipt_id=approval_receipt_id,
            approval_payload_digest=approval_payload_digest,
            plan_id=plan_id,
            plan_digest=plan_digest,
            job_id=job_id,
            execution_idempotency_key=execution_idempotency_key,
            actor_user_id=actor_user_id,
            actor_own_id=actor_own_id,
            issued_at=issued_at,
            expires_at=expires_at,
        )
        signature = self._key.sign(
            NETWORK_APPROVAL_DOMAIN + b"\x00" + unsigned.canonical_bytes_for_signing()
        ).hex()
        return unsigned.with_signature(signature)


class NetworkApprovalVerifier:
    """Verifier pinned by the native agent; it never receives the private seed."""

    def __init__(self, public_key: bytes, *, max_clock_skew_sec: int = 30) -> None:
        if not isinstance(public_key, bytes) or len(public_key) != 32:
            raise ValueError("network approval Ed25519 public key must contain 32 bytes")
        if not 0 <= max_clock_skew_sec <= 60:
            raise ValueError("network approval clock skew is invalid")
        self._public_key = bytes(public_key)
        self._key = Ed25519PublicKey.from_public_bytes(public_key)
        self.max_clock_skew_sec = int(max_clock_skew_sec)

    @property
    def public_key_digest(self) -> str:
        return hashlib.sha256(self._public_key).hexdigest()

    def verify(self, proof: NetworkApprovalProof, *, now: int) -> None:
        if isinstance(now, bool) or not isinstance(now, int):
            raise NetworkApprovalError("network_approval_time_invalid")
        if proof.issued_at > now + self.max_clock_skew_sec:
            raise NetworkApprovalError("network_approval_from_future")
        if proof.expires_at <= now:
            raise NetworkApprovalError("network_approval_expired")
        if not proof.signature:
            raise NetworkApprovalError("network_approval_signature_invalid")
        try:
            self._key.verify(
                bytes.fromhex(proof.signature),
                NETWORK_APPROVAL_DOMAIN + b"\x00" + proof.canonical_bytes_for_signing(),
            )
        except (InvalidSignature, ValueError) as exc:
            raise NetworkApprovalError("network_approval_signature_invalid") from exc


def assert_network_approval_binding(
    proof: NetworkApprovalProof,
    *,
    host_agent_id: str,
    approval_receipt_id: str | None,
    approval_payload_digest: str,
    plan_id: str,
    plan_digest: str,
    plan_created_at: int,
    plan_expires_at: int,
    job_id: str,
    execution_idempotency_key: str,
    actor_user_id: str,
    actor_own_id: str,
) -> None:
    """Bind a valid signature to the exact immutable agent request."""

    if (
        approval_receipt_id is None
        or proof.host_agent_id != host_agent_id
        or proof.approval_receipt_id != approval_receipt_id
        or proof.approval_payload_digest != approval_payload_digest
        or proof.plan_id != plan_id
        or proof.plan_digest != plan_digest
        or proof.job_id != job_id
        or proof.execution_idempotency_key != execution_idempotency_key
        or proof.actor_user_id != actor_user_id
        or proof.actor_own_id != actor_own_id
        or proof.issued_at < plan_created_at
        or proof.expires_at > plan_expires_at
    ):
        raise NetworkApprovalError("network_approval_binding_mismatch")


def load_network_approval_public_key(path: str | Path) -> bytes:
    """Read the native agent's root-owned pinned verification key."""

    return load_broker_approval_public_key(path)


class NetworkApprovalLedger:
    """Durable one-proof/one-job claim ledger for native agent admission."""

    def __init__(self, database: str | Path) -> None:
        selected = Path(database)
        if (
            not selected.is_absolute()
            or selected.is_symlink()
            or selected.parent.is_symlink()
            or selected.parent.resolve(strict=True) != selected.parent
        ):
            raise ValueError("network approval ledger path must be canonical and absolute")
        if selected.exists():
            observed = selected.lstat()
            if (
                not stat.S_ISREG(observed.st_mode)
                or observed.st_uid != os.geteuid()
                or observed.st_nlink != 1
                or observed.st_mode & (stat.S_IRWXG | stat.S_IRWXO)
            ):
                raise ValueError("network approval ledger has unsafe metadata")
        self._lock = threading.Lock()
        self._connection = sqlite3.connect(str(selected), check_same_thread=False, isolation_level=None)
        self._connection.row_factory = sqlite3.Row
        os.chmod(selected, 0o600)
        self._connection.execute("PRAGMA busy_timeout=5000")
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA synchronous=FULL")
        self._connection.execute(
            """CREATE TABLE IF NOT EXISTS consumed_network_approvals (
                   proof_id TEXT PRIMARY KEY,
                   proof_digest TEXT NOT NULL,
                   job_id TEXT NOT NULL UNIQUE,
                   plan_digest TEXT NOT NULL,
                   execution_idempotency_key TEXT NOT NULL,
                   actor_user_id TEXT NOT NULL,
                   actor_own_id TEXT NOT NULL,
                   approval_receipt_id TEXT NOT NULL,
                   expires_at INTEGER NOT NULL,
                   consumed_at INTEGER NOT NULL
               )"""
        )
        self._connection.execute(
            """CREATE TRIGGER IF NOT EXISTS consumed_network_approvals_immutable
               BEFORE UPDATE ON consumed_network_approvals
               BEGIN SELECT RAISE(ABORT, 'network approval claim is immutable'); END"""
        )
        self._connection.execute(
            """CREATE TRIGGER IF NOT EXISTS consumed_network_approvals_no_delete
               BEFORE DELETE ON consumed_network_approvals
               BEGIN SELECT RAISE(ABORT, 'network approval claim cannot be deleted'); END"""
        )

    @staticmethod
    def _values(proof: NetworkApprovalProof) -> tuple[Any, ...]:
        return (
            proof.proof_id,
            proof.digest,
            proof.job_id,
            proof.plan_digest,
            proof.execution_idempotency_key,
            proof.actor_user_id,
            proof.actor_own_id,
            proof.approval_receipt_id,
            proof.expires_at,
        )

    @staticmethod
    def _row_values(row: sqlite3.Row) -> tuple[Any, ...]:
        return tuple(
            row[name]
            for name in (
                "proof_id",
                "proof_digest",
                "job_id",
                "plan_digest",
                "execution_idempotency_key",
                "actor_user_id",
                "actor_own_id",
                "approval_receipt_id",
                "expires_at",
            )
        )

    def claim(self, proof: NetworkApprovalProof, *, now: int) -> bool:
        """Consume a proof once; exact retries retain idempotent job lookup."""

        if isinstance(now, bool) or not isinstance(now, int) or proof.expires_at <= now:
            raise NetworkApprovalError("network_approval_expired")
        values = self._values(proof)
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                existing = self._connection.execute(
                    """SELECT * FROM consumed_network_approvals
                       WHERE proof_id=? OR job_id=?""",
                    (proof.proof_id, proof.job_id),
                ).fetchone()
                if existing is not None:
                    if self._row_values(existing) != values:
                        raise NetworkApprovalError("network_approval_replayed")
                    self._connection.execute("COMMIT")
                    return False
                self._connection.execute(
                    """INSERT INTO consumed_network_approvals(
                           proof_id,proof_digest,job_id,plan_digest,
                           execution_idempotency_key,actor_user_id,actor_own_id,
                           approval_receipt_id,expires_at,consumed_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (*values, now),
                )
                self._connection.execute("COMMIT")
                return True
            except NetworkApprovalError:
                self._connection.execute("ROLLBACK")
                raise
            except sqlite3.IntegrityError as exc:
                self._connection.execute("ROLLBACK")
                raise NetworkApprovalError("network_approval_replayed") from exc
            except BaseException:
                if self._connection.in_transaction:
                    self._connection.execute("ROLLBACK")
                raise

    def assert_claimed(self, proof: NetworkApprovalProof) -> None:
        """Recheck the immutable durable claim immediately before process launch."""

        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM consumed_network_approvals WHERE proof_id=? OR job_id=?",
                (proof.proof_id, proof.job_id),
            ).fetchone()
        if row is None:
            raise NetworkApprovalError("network_approval_claim_missing")
        if self._row_values(row) != self._values(proof):
            raise NetworkApprovalError("network_approval_replayed")

    def close(self) -> None:
        with self._lock:
            self._connection.close()


__all__ = [
    "MAX_NETWORK_APPROVAL_TTL_SEC",
    "NETWORK_APPROVAL_DOMAIN",
    "NETWORK_APPROVAL_SCHEMA_VERSION",
    "NetworkApprovalError",
    "NetworkApprovalLedger",
    "NetworkApprovalProof",
    "NetworkApprovalSigner",
    "NetworkApprovalVerifier",
    "assert_network_approval_binding",
    "load_network_approval_public_key",
]
