"""Immutable host-action receipts and post-execution drift verification."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .adapters.base import ExecutionSpec
from .contracts import (
    RECEIPT_SCHEMA_VERSION,
    ContractError,
    EffectOutcome,
    EvidenceRef,
    ExecutableAttestation,
    canonical_digest,
    canonical_json_bytes,
)
from .plans import HostActionPlan

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
SignatureVerifier = Callable[[str, bytes, str], bool]


@dataclass(frozen=True, slots=True)
class ProcessObservation:
    started_at: int
    finished_at: int | None
    exit_code: int | None
    signal: int | None
    timed_out: bool
    cancellation_requested: bool
    termination_observed: bool
    stdout_truncated: bool
    stderr_truncated: bool
    effect_boundary_crossed: bool = False
    unit_id: str | None = None
    cgroup_identity: str | None = None

    def __post_init__(self) -> None:
        if isinstance(self.started_at, bool) or not isinstance(self.started_at, int) or self.started_at < 0:
            raise ContractError("process start timestamp is invalid")
        if self.finished_at is not None and (
            isinstance(self.finished_at, bool)
            or not isinstance(self.finished_at, int)
            or self.finished_at < self.started_at
        ):
            raise ContractError("process finish timestamp is invalid")
        if self.exit_code is not None and (
            isinstance(self.exit_code, bool) or not -255 <= self.exit_code <= 255
        ):
            raise ContractError("process exit code is invalid")
        if self.signal is not None and (isinstance(self.signal, bool) or not 1 <= self.signal <= 127):
            raise ContractError("process signal is invalid")
        if self.exit_code is not None and self.signal is not None:
            raise ContractError("process cannot have both exit code and signal")
        if self.finished_at is None and (self.exit_code is not None or self.signal is not None):
            raise ContractError("unfinished process has a terminal status")
        if not isinstance(self.effect_boundary_crossed, bool):
            raise ContractError("process effect-boundary marker is invalid")
        for value in (self.unit_id, self.cgroup_identity):
            if value is not None and (not _ID.fullmatch(value) or len(value) > 128):
                raise ContractError("process/cgroup identity is invalid")

    def to_payload(self) -> dict[str, Any]:
        return {
            "cancellation_requested": self.cancellation_requested,
            "exit_code": self.exit_code,
            "effect_boundary_crossed": self.effect_boundary_crossed,
            "finished_at": self.finished_at,
            "cgroup_identity": self.cgroup_identity,
            "signal": self.signal,
            "started_at": self.started_at,
            "stderr_truncated": self.stderr_truncated,
            "stdout_truncated": self.stdout_truncated,
            "termination_observed": self.termination_observed,
            "timed_out": self.timed_out,
            "unit_id": self.unit_id,
        }

    @classmethod
    def from_payload(cls, value: Any) -> ProcessObservation:
        if not isinstance(value, dict) or set(value) != set(cls.__dataclass_fields__):
            raise ContractError("process observation fields are invalid")
        try:
            return cls(**value)
        except TypeError as exc:
            raise ContractError("process observation field types are invalid") from exc


@dataclass(frozen=True, slots=True)
class HostActionReceipt:
    schema_version: int
    protocol_version: str
    host_agent_id: str
    host_agent_version: str
    job_id: str
    idempotency_key: str
    plan_digest: str
    adapter_id: str
    executable_attestation: ExecutableAttestation
    argv_sha256: str
    argv_rendering: tuple[str, ...]
    target_snapshot_digest: str | None
    process: ProcessObservation
    evidence: tuple[EvidenceRef, ...]
    parsed_result_digest: str | None
    effect_outcome: EffectOutcome
    postconditions: tuple[str, ...]
    agent_signature: str

    def __post_init__(self) -> None:
        if self.schema_version != RECEIPT_SCHEMA_VERSION or self.protocol_version != "1.0":
            raise ContractError("host action receipt version is unsupported")
        for value in (self.host_agent_id, self.job_id, self.idempotency_key, self.adapter_id):
            if not _ID.fullmatch(value):
                raise ContractError("host action receipt identity is invalid")
        if not self.host_agent_version or len(self.host_agent_version) > 120:
            raise ContractError("host agent version is invalid")
        for value in (self.plan_digest, self.argv_sha256):
            if not _DIGEST.fullmatch(value):
                raise ContractError("host action receipt digest is invalid")
        if self.target_snapshot_digest is not None and not _DIGEST.fullmatch(self.target_snapshot_digest):
            raise ContractError("receipt target snapshot digest is invalid")
        if self.parsed_result_digest is not None and not _DIGEST.fullmatch(self.parsed_result_digest):
            raise ContractError("receipt parsed result digest is invalid")
        if not _DIGEST.fullmatch(self.agent_signature):
            raise ContractError("receipt signature is invalid")
        if (
            not self.argv_rendering
            or len(self.argv_rendering) > 256
            or any(not item or len(item) > 512 or "\x00" in item for item in self.argv_rendering)
        ):
            raise ContractError("receipt argv rendering is invalid")
        if (
            len(self.evidence) > 16
            or len(self.postconditions) > 32
            or any(not item or len(item) > 160 for item in self.postconditions)
        ):
            raise ContractError("receipt evidence/postconditions are invalid")
        terminal = self.process.finished_at is not None
        if self.effect_outcome is EffectOutcome.SUCCEEDED and (
            not terminal or self.process.exit_code != 0 or self.parsed_result_digest is None
        ):
            raise ContractError("successful receipt lacks terminal semantic evidence")
        if not terminal and self.effect_outcome not in {EffectOutcome.UNKNOWN, EffectOutcome.CANCELLED}:
            raise ContractError("nonterminal receipt must remain unknown/cancelled")

    def unsigned_payload(self) -> dict[str, Any]:
        return {
            "adapter_id": self.adapter_id,
            "argv_rendering": list(self.argv_rendering),
            "argv_sha256": self.argv_sha256,
            "effect_outcome": self.effect_outcome.value,
            "evidence": [item.to_payload() for item in self.evidence],
            "executable_attestation": self.executable_attestation.to_payload(),
            "host_agent_id": self.host_agent_id,
            "host_agent_version": self.host_agent_version,
            "idempotency_key": self.idempotency_key,
            "job_id": self.job_id,
            "parsed_result_digest": self.parsed_result_digest,
            "plan_digest": self.plan_digest,
            "postconditions": list(self.postconditions),
            "process": self.process.to_payload(),
            "protocol_version": self.protocol_version,
            "schema_version": self.schema_version,
            "target_snapshot_digest": self.target_snapshot_digest,
        }

    def signing_bytes(self) -> bytes:
        return canonical_json_bytes(self.unsigned_payload())

    def to_payload(self) -> dict[str, Any]:
        return {**self.unsigned_payload(), "agent_signature": self.agent_signature}

    @classmethod
    def from_payload(cls, value: Any) -> HostActionReceipt:
        expected = {
            "adapter_id",
            "agent_signature",
            "argv_rendering",
            "argv_sha256",
            "effect_outcome",
            "evidence",
            "executable_attestation",
            "host_agent_id",
            "host_agent_version",
            "idempotency_key",
            "job_id",
            "parsed_result_digest",
            "plan_digest",
            "postconditions",
            "process",
            "protocol_version",
            "schema_version",
            "target_snapshot_digest",
        }
        if not isinstance(value, dict) or set(value) != expected:
            raise ContractError("host action receipt fields are invalid")
        evidence = value.get("evidence")
        argv = value.get("argv_rendering")
        postconditions = value.get("postconditions")
        if (
            not isinstance(evidence, list)
            or not isinstance(argv, list)
            or not isinstance(postconditions, list)
        ):
            raise ContractError("host action receipt collections are invalid")
        try:
            return cls(
                schema_version=value["schema_version"],
                protocol_version=value["protocol_version"],
                host_agent_id=value["host_agent_id"],
                host_agent_version=value["host_agent_version"],
                job_id=value["job_id"],
                idempotency_key=value["idempotency_key"],
                plan_digest=value["plan_digest"],
                adapter_id=value["adapter_id"],
                executable_attestation=ExecutableAttestation.from_payload(value["executable_attestation"]),
                argv_sha256=value["argv_sha256"],
                argv_rendering=tuple(argv),
                target_snapshot_digest=value["target_snapshot_digest"],
                process=ProcessObservation.from_payload(value["process"]),
                evidence=tuple(EvidenceRef.from_payload(item) for item in evidence),
                parsed_result_digest=value["parsed_result_digest"],
                effect_outcome=EffectOutcome(value["effect_outcome"]),
                postconditions=tuple(postconditions),
                agent_signature=value["agent_signature"],
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ContractError("host action receipt payload is invalid") from exc

    @property
    def digest(self) -> str:
        return canonical_digest(self.to_payload())


@dataclass(frozen=True, slots=True)
class ReceiptVerification:
    receipt_digest: str
    outcome: EffectOutcome
    postconditions_satisfied: bool


def verify_action_receipt(
    receipt: HostActionReceipt,
    *,
    plan: HostActionPlan,
    execution: ExecutionSpec,
    signature_verifier: SignatureVerifier,
) -> ReceiptVerification:
    if (
        receipt.plan_digest != plan.digest
        or receipt.host_agent_id != plan.host_agent_id
        or receipt.idempotency_key != plan.idempotency_key
        or receipt.adapter_id != plan.adapter_id
        or receipt.executable_attestation.digest != plan.executable_attestation_digest
        or receipt.argv_sha256 != execution.argv_sha256
        or receipt.target_snapshot_digest != plan.target_snapshot_digest
    ):
        raise ContractError("host action receipt drifted from its admitted plan")
    if not signature_verifier(receipt.host_agent_id, receipt.signing_bytes(), receipt.agent_signature):
        raise ContractError("host action receipt signature is invalid")
    postconditions_satisfied = bool(
        receipt.effect_outcome is EffectOutcome.SUCCEEDED
        and receipt.process.exit_code == 0
        and receipt.parsed_result_digest
        and receipt.postconditions
    )
    return ReceiptVerification(
        receipt_digest=receipt.digest,
        outcome=receipt.effect_outcome,
        postconditions_satisfied=postconditions_satisfied,
    )


__all__ = [
    "HostActionReceipt",
    "ProcessObservation",
    "ReceiptVerification",
    "SignatureVerifier",
    "verify_action_receipt",
]
