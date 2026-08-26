"""Versioned, immutable backend contracts for the host capability plane.

This package deliberately describes plans and evidence only.  It has no process
creation primitive and does not resolve executables from ``PATH``.
"""

from __future__ import annotations

import hashlib
import json
import math
import posixpath
import re
from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Any

PROTOCOL_VERSION = "1.0"
PROTOCOL_MAJOR = 1
MAX_WIRE_BYTES = 1024 * 1024
MAX_BODY_BYTES = 512 * 1024
PLAN_SCHEMA_VERSION = 1
RESULT_SCHEMA_VERSION = 1
RECEIPT_SCHEMA_VERSION = 1

_OPAQUE_ID = re.compile(r"^[a-z][a-z0-9_]{1,31}_[0-9a-f]{16,64}$")
_WIRE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_WIRE_METHOD = re.compile(r"^[A-Z][A-Za-z0-9]{0,63}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_TOKEN = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")

JsonScalar = None | bool | int | str
FrozenJson = JsonScalar | tuple["FrozenJson", ...] | tuple[tuple[str, "FrozenJson"], ...]


class ContractError(ValueError):
    """A wire or durable contract failed closed validation."""


class AdapterState(StrEnum):
    AVAILABLE = "available"
    MISSING_PACKAGE = "missing_package"
    UNSUPPORTED_VERSION = "unsupported_version"
    NEEDS_SETUP = "needs_setup"
    DISABLED = "disabled"
    QUARANTINED = "quarantined"
    UNATTESTED = "unattested"


class RiskClass(StrEnum):
    LOCAL_READONLY = "local_readonly"
    WORKSPACE_TRANSFORM = "workspace_transform"
    NETWORK_OBSERVE = "network_observe"
    PACKAGE_MUTATION = "package_mutation"


class ExecutionProfile(StrEnum):
    CLI_LOCAL_READONLY = "cli_local_readonly"
    CLI_WORKSPACE_TRANSFORM = "cli_workspace_transform"
    CLI_NETWORK_UNPRIVILEGED = "cli_network_unprivileged"
    PACKAGE_TRANSACTION = "package_transaction"


class ParserStatus(StrEnum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    UNAVAILABLE = "unavailable"


class CoverageGrade(StrEnum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    UNAVAILABLE = "unavailable"


class EffectOutcome(StrEnum):
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


def _validated_token(value: str, *, field: str) -> str:
    text = str(value or "")
    if not _TOKEN.fullmatch(text):
        raise ContractError(f"{field} is invalid")
    return text


def _validated_opaque_id(value: str, *, field: str) -> str:
    text = str(value or "")
    if not _OPAQUE_ID.fullmatch(text):
        raise ContractError(f"{field} is invalid")
    return text


def _validated_sha256(value: str, *, field: str) -> str:
    text = str(value or "")
    if not _SHA256.fullmatch(text):
        raise ContractError(f"{field} is invalid")
    return text


def _plain_json(value: Any, *, depth: int = 0) -> Any:
    if depth > 12:
        raise ContractError("JSON contract nesting exceeds limit")
    if value is None or isinstance(value, bool | str):
        return value
    if isinstance(value, int):
        if abs(value) > 2**63 - 1:
            raise ContractError("JSON integer exceeds signed 64-bit range")
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ContractError("non-finite values are not canonical contract data")
        return value
    if isinstance(value, dict):
        if len(value) > 256:
            raise ContractError("JSON object exceeds member limit")
        output: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str) or not key or len(key) > 128:
                raise ContractError("JSON object key is invalid")
            output[key] = _plain_json(item, depth=depth + 1)
        return output
    if isinstance(value, list | tuple):
        if len(value) > 4096:
            raise ContractError("JSON array exceeds item limit")
        return [_plain_json(item, depth=depth + 1) for item in value]
    raise ContractError(f"unsupported canonical JSON type: {type(value).__name__}")


def canonical_json_bytes(value: Any, *, maximum: int = 512 * 1024) -> bytes:
    """Encode one strict JSON value identically across plan/receipt boundaries."""

    encoded = json.dumps(
        _plain_json(value),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(encoded) > maximum:
        raise ContractError("canonical JSON exceeds byte limit")
    return encoded


def canonical_digest(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def decode_canonical_json(value: bytes, *, maximum: int = 512 * 1024) -> Any:
    if not isinstance(value, bytes) or len(value) > maximum:
        raise ContractError("canonical JSON bytes are invalid")
    try:
        decoded = json.loads(value.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ContractError("canonical JSON is invalid") from exc
    canonical = canonical_json_bytes(decoded, maximum=maximum)
    if canonical != value:
        raise ContractError("JSON bytes are not canonical")
    return decoded


def body_sha256(body: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(body, maximum=MAX_BODY_BYTES)).hexdigest()


@dataclass(frozen=True, slots=True)
class RequestEnvelope:
    """Authenticated host-agent request metadata shared by both trust domains."""

    protocol_version: str
    request_id: str
    agent_id: str
    sequence: int
    issued_at: int
    expires_at: int
    method: str
    job_id: str
    actor_id: str
    own_id: str
    idempotency_key: str
    plan_digest: str
    approval_receipt_id: str | None
    body_sha256: str
    signature: str

    def __post_init__(self) -> None:
        if self.protocol_version != PROTOCOL_VERSION:
            raise ContractError("unsupported host-control protocol")
        for name in ("request_id", "agent_id", "job_id", "actor_id", "own_id", "idempotency_key"):
            if not _WIRE_IDENTIFIER.fullmatch(str(getattr(self, name))):
                raise ContractError(f"wire {name} is invalid")
        if not _WIRE_METHOD.fullmatch(self.method):
            raise ContractError("wire method is invalid")
        if isinstance(self.sequence, bool) or self.sequence < 0:
            raise ContractError("wire sequence is invalid")
        if any(
            isinstance(value, bool) or not isinstance(value, int)
            for value in (self.issued_at, self.expires_at)
        ):
            raise ContractError("wire timestamps are invalid")
        if self.expires_at <= self.issued_at:
            raise ContractError("wire expiry is invalid")
        _validated_sha256(self.plan_digest, field="wire plan digest")
        _validated_sha256(self.body_sha256, field="wire body digest")
        if self.approval_receipt_id is not None and not _WIRE_IDENTIFIER.fullmatch(self.approval_receipt_id):
            raise ContractError("wire approval receipt id is invalid")
        if self.signature and not _SHA256.fullmatch(self.signature):
            raise ContractError("wire signature is invalid")

    def unsigned_payload(self) -> dict[str, Any]:
        payload = asdict(self)
        payload.pop("signature")
        return payload

    def signing_bytes(self) -> bytes:
        return canonical_json_bytes(self.unsigned_payload(), maximum=MAX_BODY_BYTES)

    def to_payload(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_payload(cls, value: Any) -> RequestEnvelope:
        if not isinstance(value, dict) or set(value) != set(cls.__dataclass_fields__):
            raise ContractError("wire envelope fields are invalid")
        try:
            return cls(**value)
        except TypeError as exc:
            raise ContractError("wire envelope field types are invalid") from exc


@dataclass(frozen=True, slots=True)
class WireRequest:
    envelope: RequestEnvelope
    body_json: bytes

    def __post_init__(self) -> None:
        body = decode_canonical_json(self.body_json, maximum=MAX_BODY_BYTES)
        if not isinstance(body, dict):
            raise ContractError("wire request body must be an object")
        if body_sha256(body) != self.envelope.body_sha256:
            raise ContractError("wire request body digest mismatch")

    @property
    def body(self) -> dict[str, Any]:
        body = decode_canonical_json(self.body_json, maximum=MAX_BODY_BYTES)
        assert isinstance(body, dict)
        return body

    @classmethod
    def create(cls, envelope: RequestEnvelope, body: dict[str, Any]) -> WireRequest:
        return cls(envelope=envelope, body_json=canonical_json_bytes(body, maximum=MAX_BODY_BYTES))

    @classmethod
    def decode(cls, raw: bytes) -> WireRequest:
        if not isinstance(raw, bytes) or len(raw) > MAX_WIRE_BYTES:
            raise ContractError("wire request exceeds byte limit")
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
            raise ContractError("wire request is invalid JSON") from exc
        if not isinstance(value, dict) or set(value) != {"body", "envelope"}:
            raise ContractError("wire request fields are invalid")
        if not isinstance(value["body"], dict):
            raise ContractError("wire request body must be an object")
        return cls.create(RequestEnvelope.from_payload(value["envelope"]), value["body"])

    def encode(self) -> bytes:
        return canonical_json_bytes(
            {"body": self.body, "envelope": self.envelope.to_payload()},
            maximum=MAX_WIRE_BYTES,
        )


@dataclass(frozen=True, slots=True)
class EvidenceRef:
    evidence_id: str
    sha256: str
    size_bytes: int
    media_type: str

    def __post_init__(self) -> None:
        _validated_opaque_id(self.evidence_id, field="evidence_id")
        _validated_sha256(self.sha256, field="evidence sha256")
        if isinstance(self.size_bytes, bool) or not 0 <= self.size_bytes <= 64 * 1024 * 1024:
            raise ContractError("evidence size is invalid")
        if not re.fullmatch(r"[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+", self.media_type):
            raise ContractError("evidence media type is invalid")

    def to_payload(self) -> dict[str, Any]:
        return {
            "evidence_id": self.evidence_id,
            "media_type": self.media_type,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
        }

    @classmethod
    def from_payload(cls, value: Any) -> EvidenceRef:
        if not isinstance(value, dict) or set(value) != set(cls.__dataclass_fields__):
            raise ContractError("evidence reference fields are invalid")
        try:
            return cls(**value)
        except TypeError as exc:
            raise ContractError("evidence reference field types are invalid") from exc


@dataclass(frozen=True, slots=True)
class ExecutableAttestation:
    """Exact executable identity observed by the host agent."""

    schema_version: int
    canonical_path: str
    device: int
    inode: int
    mode: int
    owner_uid: int
    owner_gid: int
    size_bytes: int
    mtime_ns: int
    sha256: str
    package_name: str
    package_version: str
    architecture: str
    observed_version: str
    adapter_id: str
    adapter_schema_version: int
    implementation_version: int

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ContractError("unknown executable attestation schema")
        if (
            not self.canonical_path.startswith("/")
            or "\x00" in self.canonical_path
            or posixpath.normpath(self.canonical_path) != self.canonical_path
        ):
            raise ContractError("executable path must be canonical and absolute")
        if len(self.canonical_path) > 512 or "/../" in f"{self.canonical_path}/":
            raise ContractError("executable path is invalid")
        if not re.fullmatch(r"[a-z0-9][a-z0-9+.-]{0,127}", self.package_name):
            raise ContractError("package_name is invalid")
        if not 1 <= len(self.package_version) <= 160:
            raise ContractError("package_version is invalid")
        if not re.fullmatch(r"[A-Za-z0-9_.+-]{1,32}", self.architecture):
            raise ContractError("architecture is invalid")
        _validated_token(self.adapter_id, field="attestation adapter_id")
        if not self.observed_version or len(self.observed_version) > 240:
            raise ContractError("observed executable version is invalid")
        integer_fields = (
            self.device,
            self.inode,
            self.mode,
            self.owner_uid,
            self.owner_gid,
            self.size_bytes,
            self.mtime_ns,
            self.adapter_schema_version,
            self.implementation_version,
        )
        if any(isinstance(item, bool) or item < 0 for item in integer_fields):
            raise ContractError("executable numeric identity is invalid")
        if self.adapter_schema_version != 1 or self.implementation_version < 1:
            raise ContractError("executable adapter version identity is invalid")
        if not 0 < self.mode <= 0o177777 or not 0 < self.size_bytes <= 2**63 - 1:
            raise ContractError("executable stat identity is invalid")
        _validated_sha256(self.sha256, field="executable sha256")

    def to_payload(self) -> dict[str, Any]:
        return {
            "adapter_id": self.adapter_id,
            "adapter_schema_version": self.adapter_schema_version,
            "architecture": self.architecture,
            "canonical_path": self.canonical_path,
            "device": self.device,
            "implementation_version": self.implementation_version,
            "inode": self.inode,
            "mode": self.mode,
            "mtime_ns": self.mtime_ns,
            "observed_version": self.observed_version,
            "owner_gid": self.owner_gid,
            "owner_uid": self.owner_uid,
            "package_name": self.package_name,
            "package_version": self.package_version,
            "schema_version": self.schema_version,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
        }

    @classmethod
    def from_payload(cls, value: Any) -> ExecutableAttestation:
        if not isinstance(value, dict) or set(value) != set(cls.__dataclass_fields__):
            raise ContractError("executable attestation fields are invalid")
        try:
            return cls(**value)
        except TypeError as exc:
            raise ContractError("executable attestation field types are invalid") from exc

    @property
    def digest(self) -> str:
        return canonical_digest(self.to_payload())


@dataclass(frozen=True, slots=True)
class Coverage:
    grade: CoverageGrade
    requested: int
    accounted: int
    skipped: int = 0
    reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if any(
            isinstance(value, bool) or not 0 <= value <= 2**31 - 1
            for value in (self.requested, self.accounted, self.skipped)
        ):
            raise ContractError("coverage count is invalid")
        if self.accounted + self.skipped > self.requested:
            raise ContractError("coverage accounting exceeds request")
        if len(self.reasons) > 16 or any(not item or len(item) > 120 for item in self.reasons):
            raise ContractError("coverage reasons are invalid")
        if self.grade is CoverageGrade.COMPLETE and (
            self.requested == 0 or self.accounted != self.requested or self.skipped
        ):
            raise ContractError("complete coverage does not close target accounting")

    def to_payload(self) -> dict[str, Any]:
        return {
            "accounted": self.accounted,
            "grade": self.grade.value,
            "reasons": list(self.reasons),
            "requested": self.requested,
            "skipped": self.skipped,
        }

    @classmethod
    def from_payload(cls, value: Any) -> Coverage:
        if not isinstance(value, dict) or set(value) != {
            "accounted",
            "grade",
            "reasons",
            "requested",
            "skipped",
        }:
            raise ContractError("coverage fields are invalid")
        if not isinstance(value.get("reasons"), list):
            raise ContractError("coverage reasons are invalid")
        try:
            return cls(
                grade=CoverageGrade(value["grade"]),
                requested=value["requested"],
                accounted=value["accounted"],
                skipped=value["skipped"],
                reasons=tuple(value["reasons"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ContractError("coverage payload is invalid") from exc


@dataclass(frozen=True, slots=True)
class ParsedActionResult:
    schema_version: int
    parser_id: str
    parser_status: ParserStatus
    structured_json: bytes
    coverage: Coverage
    warnings: tuple[str, ...] = ()
    evidence: tuple[EvidenceRef, ...] = ()

    def __post_init__(self) -> None:
        if self.schema_version != RESULT_SCHEMA_VERSION:
            raise ContractError("unknown action result schema")
        _validated_token(self.parser_id, field="parser_id")
        decode_canonical_json(self.structured_json)
        if len(self.warnings) > 32 or any(len(item) > 512 for item in self.warnings):
            raise ContractError("result warnings are invalid")
        if len(self.evidence) > 16:
            raise ContractError("too many result evidence references")
        if self.parser_status is ParserStatus.COMPLETE and self.coverage.grade is CoverageGrade.UNAVAILABLE:
            raise ContractError("complete parser cannot have unavailable coverage")

    @classmethod
    def create(
        cls,
        *,
        parser_id: str,
        parser_status: ParserStatus,
        structured: Any,
        coverage: Coverage,
        warnings: tuple[str, ...] = (),
        evidence: tuple[EvidenceRef, ...] = (),
    ) -> ParsedActionResult:
        return cls(
            schema_version=RESULT_SCHEMA_VERSION,
            parser_id=parser_id,
            parser_status=parser_status,
            structured_json=canonical_json_bytes(structured),
            coverage=coverage,
            warnings=warnings,
            evidence=evidence,
        )

    @property
    def structured(self) -> Any:
        return decode_canonical_json(self.structured_json)

    @property
    def digest(self) -> str:
        return canonical_digest(
            {
                "coverage": self.coverage.to_payload(),
                "evidence": [item.to_payload() for item in self.evidence],
                "parser_id": self.parser_id,
                "parser_status": self.parser_status.value,
                "schema_version": self.schema_version,
                "structured": self.structured,
                "warnings": list(self.warnings),
            }
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "coverage": self.coverage.to_payload(),
            "evidence": [item.to_payload() for item in self.evidence],
            "parser_id": self.parser_id,
            "parser_status": self.parser_status.value,
            "schema_version": self.schema_version,
            "structured": self.structured,
            "warnings": list(self.warnings),
        }

    @classmethod
    def from_payload(cls, value: Any) -> ParsedActionResult:
        expected = {
            "coverage",
            "evidence",
            "parser_id",
            "parser_status",
            "schema_version",
            "structured",
            "warnings",
        }
        if (
            not isinstance(value, dict)
            or set(value) != expected
            or not isinstance(value.get("evidence"), list)
            or not isinstance(value.get("warnings"), list)
        ):
            raise ContractError("parsed action result fields are invalid")
        try:
            return cls(
                schema_version=value["schema_version"],
                parser_id=value["parser_id"],
                parser_status=ParserStatus(value["parser_status"]),
                structured_json=canonical_json_bytes(value["structured"]),
                coverage=Coverage.from_payload(value["coverage"]),
                warnings=tuple(value["warnings"]),
                evidence=tuple(EvidenceRef.from_payload(item) for item in value["evidence"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ContractError("parsed action result payload is invalid") from exc


__all__ = [
    "AdapterState",
    "ContractError",
    "Coverage",
    "CoverageGrade",
    "EffectOutcome",
    "EvidenceRef",
    "ExecutableAttestation",
    "ExecutionProfile",
    "PLAN_SCHEMA_VERSION",
    "PROTOCOL_MAJOR",
    "PROTOCOL_VERSION",
    "RequestEnvelope",
    "ParsedActionResult",
    "ParserStatus",
    "RECEIPT_SCHEMA_VERSION",
    "RESULT_SCHEMA_VERSION",
    "RiskClass",
    "WireRequest",
    "body_sha256",
    "canonical_digest",
    "canonical_json_bytes",
    "decode_canonical_json",
]
