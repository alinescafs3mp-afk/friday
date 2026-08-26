"""Host-side construction and signing of the one shared receipt DTO."""

from __future__ import annotations

import re
from dataclasses import replace
from urllib.parse import urlsplit, urlunsplit

from friday.host_control.adapters.base import ExecutionSpec
from friday.host_control.contracts import (
    RECEIPT_SCHEMA_VERSION,
    EffectOutcome,
    EvidenceRef,
    ExecutableAttestation,
)
from friday.host_control.plans import HostActionPlan
from friday.host_control.receipts import HostActionReceipt, ProcessObservation

from .authentication import HMACAuthenticator
from .process_runner import ProcessResult

_SECRET_FLAG = re.compile(r"(?i)(password|passwd|token|secret|api[_-]?key)")


class ReceiptSigner:
    def __init__(self, authenticator: HMACAuthenticator) -> None:
        self._authenticator = authenticator

    def sign(self, receipt: HostActionReceipt) -> HostActionReceipt:
        signature = self._authenticator.sign_bytes(
            receipt.signing_bytes(), domain=b"friday-host-agent-receipt-v1"
        )
        return replace(receipt, agent_signature=signature)

    def verify(self, receipt: HostActionReceipt) -> bool:
        return self._authenticator.verify_bytes(
            receipt.signing_bytes(),
            receipt.agent_signature,
            domain=b"friday-host-agent-receipt-v1",
        )


def build_receipt(
    *,
    job_id: str,
    plan: HostActionPlan,
    host_agent_version: str,
    executable_attestation: ExecutableAttestation,
    execution: ExecutionSpec,
    result: ProcessResult,
    evidence: tuple[EvidenceRef, ...] = (),
    parsed_result_digest: str | None = None,
    postconditions: tuple[str, ...] = (),
) -> HostActionReceipt:
    """Project raw execution into the shared semantic receipt without overclaiming.

    A zero exit by itself is only ``partial`` until an adapter parser supplies a
    result digest and observed postconditions.  Uncertain process/cgroup state
    always stays ``unknown``.
    """

    if executable_attestation.digest != plan.executable_attestation_digest:
        raise ValueError("receipt executable attestation does not match the signed plan")
    effect_outcome = _effect_outcome(result, parsed_result_digest, postconditions)
    terminal_observed = result.outcome != "unknown" and (
        result.exit_code is not None or result.signal is not None
    )
    return HostActionReceipt(
        schema_version=RECEIPT_SCHEMA_VERSION,
        protocol_version="1.0",
        host_agent_id=plan.host_agent_id,
        host_agent_version=host_agent_version,
        job_id=job_id,
        idempotency_key=plan.idempotency_key,
        plan_digest=plan.digest,
        adapter_id=plan.adapter_id,
        executable_attestation=executable_attestation,
        argv_sha256=execution.argv_sha256,
        argv_rendering=_redacted_argv(execution.argv),
        target_snapshot_digest=plan.target_snapshot_digest,
        process=ProcessObservation(
            started_at=int(result.started_at * 1_000_000_000),
            finished_at=int(result.finished_at * 1_000_000_000) if terminal_observed else None,
            exit_code=result.exit_code if terminal_observed else None,
            signal=result.signal if terminal_observed else None,
            timed_out=result.timed_out,
            cancellation_requested=result.cancelled,
            termination_observed=terminal_observed,
            stdout_truncated=result.output_truncated,
            stderr_truncated=result.output_truncated,
            effect_boundary_crossed=result.effect_boundary_crossed,
            unit_id=result.unit_id,
            cgroup_identity=result.cgroup_identity,
        ),
        evidence=evidence,
        parsed_result_digest=parsed_result_digest,
        effect_outcome=effect_outcome,
        postconditions=postconditions,
        agent_signature="0" * 64,
    )


def _effect_outcome(
    result: ProcessResult,
    parsed_result_digest: str | None,
    postconditions: tuple[str, ...],
) -> EffectOutcome:
    if result.outcome == "unknown":
        return EffectOutcome.UNKNOWN
    if result.cancelled:
        return EffectOutcome.CANCELLED
    if result.timed_out or result.exit_code not in {0, None} or result.signal is not None:
        return EffectOutcome.FAILED
    if parsed_result_digest and postconditions and not result.output_truncated:
        return EffectOutcome.SUCCEEDED
    return EffectOutcome.PARTIAL


def _redacted_argv(argv: tuple[str, ...]) -> tuple[str, ...]:
    output: list[str] = []
    hide_next = False
    for value in argv:
        if hide_next:
            output.append("[redacted]")
            hide_next = False
            continue
        cleaned = "".join(character if 32 <= ord(character) < 127 else "?" for character in value)
        if _SECRET_FLAG.search(cleaned):
            if "=" in cleaned:
                output.append(f"{cleaned.split('=', 1)[0]}=[redacted]")
            else:
                output.append(cleaned)
                hide_next = True
            continue
        if "://" in cleaned:
            try:
                parsed = urlsplit(cleaned)
                host = parsed.hostname or ""
                if parsed.port is not None:
                    host = f"{host}:{parsed.port}"
                cleaned = urlunsplit((parsed.scheme, host, parsed.path, "", ""))
            except ValueError:
                cleaned = "[redacted-url]"
        output.append(cleaned[:256])
    return tuple(output)


__all__ = ["HostActionReceipt", "ReceiptSigner", "build_receipt"]
