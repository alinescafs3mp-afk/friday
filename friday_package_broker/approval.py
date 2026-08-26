"""Backend-issued, root-broker-verifiable package approval proofs."""

from __future__ import annotations

import ctypes
import hashlib
import os
import re
import secrets
import stat
import sys
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from friday.host_control.contracts import PROTOCOL_VERSION, canonical_json_bytes

from .contracts import BrokerContractError

APPROVAL_DOMAIN = b"friday-package-install-approval-v1"
APPROVAL_PROOF_SCHEMA_VERSION = 1
MAX_APPROVAL_PROOF_TTL_SEC = 300

_PR_GET_DUMPABLE = 3
_PR_SET_DUMPABLE = 4

_ACTOR_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@+\-]{0,199}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_PROOF_ID = re.compile(r"^approvalproof_[0-9a-f]{32,64}$")
_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:\-]{0,199}$")
_SIGNATURE = re.compile(r"^[0-9a-f]{128}$")


class ApprovalProofError(BrokerContractError):
    """An approval proof is malformed, untrusted, expired, or misbound."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class PackageApprovalProof:
    """One short-lived authorization for one exact package execution attempt."""

    schema_version: int
    protocol_version: str
    proof_id: str
    broker_id: str
    approval_receipt_id: str
    approval_payload_digest: str
    plan_id: str
    plan_digest: str
    actor_user_id: str
    actor_own_id: str
    continuation_work_item_id: str
    execution_idempotency_key: str
    issued_at: int
    expires_at: int
    signature: str = ""

    def __post_init__(self) -> None:
        if self.schema_version != APPROVAL_PROOF_SCHEMA_VERSION or self.protocol_version != PROTOCOL_VERSION:
            raise ApprovalProofError("approval_version_invalid")
        if _PROOF_ID.fullmatch(self.proof_id) is None:
            raise ApprovalProofError("approval_identity_invalid")
        for value, field in (
            (self.broker_id, "broker"),
            (self.approval_receipt_id, "receipt"),
            (self.plan_id, "plan"),
            (self.continuation_work_item_id, "continuation"),
            (self.execution_idempotency_key, "idempotency"),
        ):
            if not isinstance(value, str) or _REF.fullmatch(value) is None:
                raise ApprovalProofError(f"approval_{field}_invalid")
        for value, field in (
            (self.actor_user_id, "actor"),
            (self.actor_own_id, "owner"),
        ):
            if not isinstance(value, str) or _ACTOR_ID.fullmatch(value) is None:
                raise ApprovalProofError(f"approval_{field}_invalid")
        if _DIGEST.fullmatch(self.approval_payload_digest) is None:
            raise ApprovalProofError("approval_payload_digest_invalid")
        if _DIGEST.fullmatch(self.plan_digest) is None:
            raise ApprovalProofError("approval_plan_digest_invalid")
        if any(
            isinstance(value, bool) or not isinstance(value, int)
            for value in (self.issued_at, self.expires_at)
        ):
            raise ApprovalProofError("approval_time_invalid")
        if not self.issued_at < self.expires_at <= self.issued_at + MAX_APPROVAL_PROOF_TTL_SEC:
            raise ApprovalProofError("approval_expiry_invalid")
        if self.signature and _SIGNATURE.fullmatch(self.signature) is None:
            raise ApprovalProofError("approval_signature_invalid")

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

    def with_signature(self, signature: str) -> PackageApprovalProof:
        return replace(self, signature=signature)

    @classmethod
    def from_payload(cls, value: Any) -> PackageApprovalProof:
        if not isinstance(value, dict) or set(value) != set(cls.__dataclass_fields__):
            raise ApprovalProofError("approval_fields_invalid")
        try:
            return cls(**value)
        except TypeError as exc:
            raise ApprovalProofError("approval_fields_invalid") from exc


class PackageApprovalSigner:
    """Signer available only inside the backend authorization boundary."""

    def __init__(self, private_key: bytes) -> None:
        if not isinstance(private_key, bytes) or len(private_key) != 32:
            raise ValueError("package approval Ed25519 private key must contain 32 bytes")
        self._key = Ed25519PrivateKey.from_private_bytes(private_key)

    @property
    def public_key_bytes(self) -> bytes:
        return self._key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )

    def issue(
        self,
        *,
        broker_id: str,
        approval_receipt_id: str,
        approval_payload_digest: str,
        plan_id: str,
        plan_digest: str,
        actor_user_id: str,
        actor_own_id: str,
        continuation_work_item_id: str,
        execution_idempotency_key: str,
        issued_at: int,
        expires_at: int,
    ) -> PackageApprovalProof:
        unsigned = PackageApprovalProof(
            schema_version=APPROVAL_PROOF_SCHEMA_VERSION,
            protocol_version=PROTOCOL_VERSION,
            proof_id=f"approvalproof_{secrets.token_hex(16)}",
            broker_id=broker_id,
            approval_receipt_id=approval_receipt_id,
            approval_payload_digest=approval_payload_digest,
            plan_id=plan_id,
            plan_digest=plan_digest,
            actor_user_id=actor_user_id,
            actor_own_id=actor_own_id,
            continuation_work_item_id=continuation_work_item_id,
            execution_idempotency_key=execution_idempotency_key,
            issued_at=issued_at,
            expires_at=expires_at,
        )
        signature = self._key.sign(APPROVAL_DOMAIN + b"\x00" + unsigned.canonical_bytes_for_signing()).hex()
        return unsigned.with_signature(signature)


class PackageApprovalVerifier:
    """Verifier held by the root broker; it never receives backend key material."""

    def __init__(self, public_key: bytes, *, max_clock_skew_sec: int = 30) -> None:
        if not isinstance(public_key, bytes) or len(public_key) != 32:
            raise ValueError("package approval Ed25519 public key must contain 32 bytes")
        if not 0 <= max_clock_skew_sec <= 60:
            raise ValueError("package approval clock skew is invalid")
        self._key = Ed25519PublicKey.from_public_bytes(public_key)
        self.max_clock_skew_sec = int(max_clock_skew_sec)

    def verify(self, proof: PackageApprovalProof, *, now: int) -> None:
        if proof.issued_at > now + self.max_clock_skew_sec:
            raise ApprovalProofError("approval_from_future")
        if proof.expires_at <= now:
            raise ApprovalProofError("approval_expired")
        if not proof.signature:
            raise ApprovalProofError("approval_signature_invalid")
        try:
            self._key.verify(
                bytes.fromhex(proof.signature),
                APPROVAL_DOMAIN + b"\x00" + proof.canonical_bytes_for_signing(),
            )
        except (InvalidSignature, ValueError) as exc:
            raise ApprovalProofError("approval_signature_invalid") from exc


def load_backend_approval_signing_key(path: str | Path) -> bytes:
    """Read one private seed without following links or broadening host access."""

    lock_backend_signer_process()
    return _read_exact_key(path, private=True, require_root_owner=False)


def load_broker_approval_public_key(path: str | Path) -> bytes:
    """Read the broker-pinned public key from a root-owned regular file."""

    return _read_exact_key(path, private=False, require_root_owner=True)


def lock_backend_signer_process() -> None:
    """Make signer memory and FDs inaccessible to ordinary same-UID host processes."""

    if not sys.platform.startswith("linux"):
        raise ApprovalProofError("approval_process_isolation_unavailable")
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        prctl = libc.prctl
    except (AttributeError, OSError) as exc:
        raise ApprovalProofError("approval_process_isolation_unavailable") from exc
    prctl.argtypes = [
        ctypes.c_int,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_ulong,
    ]
    prctl.restype = ctypes.c_int
    if prctl(_PR_SET_DUMPABLE, 0, 0, 0, 0) != 0:
        raise ApprovalProofError("approval_process_isolation_unavailable")
    if prctl(_PR_GET_DUMPABLE, 0, 0, 0, 0) != 0:
        raise ApprovalProofError("approval_process_isolation_unavailable")


def _read_exact_key(
    path: str | Path,
    *,
    private: bool,
    require_root_owner: bool,
) -> bytes:
    selected = Path(path)
    if not selected.is_absolute() or "\x00" in str(selected):
        raise ApprovalProofError("approval_key_path_invalid")
    try:
        if selected.resolve(strict=True) != selected:
            raise ApprovalProofError("approval_key_path_invalid")
    except OSError as exc:
        raise ApprovalProofError("approval_key_unavailable") from exc
    descriptor = -1
    try:
        descriptor = os.open(
            selected,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        before = os.fstat(descriptor)
        groups = {os.getegid(), *os.getgroups()}
        owner_ok = before.st_uid == 0 if require_root_owner else before.st_uid in {0, os.geteuid()}
        if private and before.st_uid == 0 and os.geteuid() != 0:
            permissions_ok = before.st_gid in groups and not before.st_mode & (
                stat.S_IWGRP | stat.S_IXGRP | stat.S_IRWXO
            )
        else:
            permissions_ok = not before.st_mode & (
                (stat.S_IRWXG | stat.S_IRWXO) if private else (stat.S_IWGRP | stat.S_IWOTH)
            )
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or not owner_ok
            or not permissions_ok
            or before.st_size != 32
        ):
            raise ApprovalProofError("approval_key_metadata_unsafe")
        payload = os.read(descriptor, 33)
        after = os.fstat(descriptor)
    except OSError as exc:
        raise ApprovalProofError("approval_key_unavailable") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if len(payload) != 32 or (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
    ) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ):
        raise ApprovalProofError("approval_key_changed")
    return payload


__all__ = [
    "APPROVAL_DOMAIN",
    "APPROVAL_PROOF_SCHEMA_VERSION",
    "MAX_APPROVAL_PROOF_TTL_SEC",
    "ApprovalProofError",
    "PackageApprovalProof",
    "PackageApprovalSigner",
    "PackageApprovalVerifier",
    "load_backend_approval_signing_key",
    "load_broker_approval_public_key",
    "lock_backend_signer_process",
]
