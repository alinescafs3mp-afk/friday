"""Minimal authenticated Unix-socket host-agent daemon."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import os
import platform
import socket
import stat
import struct
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from friday.host_control.adapters.jq import JqAdapter
from friday.host_control.adapters.nmap import NmapAdapter
from friday.host_control.contracts import (
    PROTOCOL_VERSION,
    ContractError,
    EffectOutcome,
    EvidenceRef,
    ParsedActionResult,
    canonical_json_bytes,
)
from friday.host_control.network_approval import (
    NetworkApprovalError,
    NetworkApprovalLedger,
    NetworkApprovalProof,
    NetworkApprovalVerifier,
    assert_network_approval_binding,
)
from friday.host_control.plans import HostActionPlan
from friday.host_control.policy import NetworkTargetSnapshot
from friday.host_control.result_projection import project_action_result
from friday_package_broker.approval import PackageApprovalProof
from friday_package_broker.client import (
    PackageBrokerClient,
    PackageBrokerRejected,
    PackageBrokerUnavailable,
    PackageBrokerUnknownOutcome,
)
from friday_package_broker.contracts import EMPTY_PLAN_DIGEST, PackageRef

from .adapter_registry import AdapterRegistry, AdapterValidationError
from .authentication import HMACAuthenticator, ReplayGuard
from .inventory import ExecutableInventory
from .job_store import AgentJobConflict, AgentJobStateError, AgentJobStore
from .process_runner import ProcessRunner, ResourceBudgets, RunnerUnavailable, WorkspaceGrant
from .protocol import MAX_WIRE_BYTES, ProtocolError, WireRequest, canonical_json
from .receipts import ReceiptSigner, build_receipt


class HostAgentDaemon:
    def __init__(
        self,
        *,
        agent_id: str,
        authenticator: HMACAuthenticator,
        replay_guard: ReplayGuard,
        inventory: ExecutableInventory,
        registry: AdapterRegistry,
        allowed_peer_uids: frozenset[int],
        build_id: str = "development",
        runner: ProcessRunner | None = None,
        job_store: AgentJobStore | None = None,
        job_root: str | Path | None = None,
        max_concurrency: int = 2,
        client_timeout_sec: float = 10.0,
        budgets: ResourceBudgets | None = None,
        package_client: PackageBrokerClient | None = None,
        network_approval_verifier: NetworkApprovalVerifier | None = None,
        network_approval_ledger: NetworkApprovalLedger | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if not allowed_peer_uids:
            raise ValueError("host agent requires an explicit UDS peer uid allowlist")
        self.agent_id = agent_id
        self._authenticator = authenticator
        self._replay_guard = replay_guard
        self._inventory = inventory
        self._registry = registry
        self._allowed_peer_uids = allowed_peer_uids
        self._build_id = build_id
        configured_execution = (runner is not None, job_store is not None, job_root is not None)
        if any(configured_execution) and not all(configured_execution):
            raise ValueError("runner, job store, and job root must be configured together")
        if isinstance(max_concurrency, bool) or not 1 <= max_concurrency <= 8:
            raise ValueError("host-agent concurrency limit is invalid")
        if not 0.05 <= float(client_timeout_sec) <= 60.0:
            raise ValueError("host-agent client timeout is invalid")
        if (network_approval_verifier is None) != (network_approval_ledger is None):
            raise ValueError("network approval verifier and ledger must be configured together")
        self._runner = runner
        self._job_store = job_store
        self._job_root = None if job_root is None else _trusted_job_root(Path(job_root))
        self._execution_slots = threading.BoundedSemaphore(max_concurrency)
        self._client_timeout_sec = float(client_timeout_sec)
        self._budgets = ResourceBudgets() if budgets is None else budgets
        self._budgets.validate()
        self._receipt_signer = ReceiptSigner(authenticator)
        self._package_client = package_client
        self._network_approval_verifier = network_approval_verifier
        self._network_approval_ledger = network_approval_ledger
        self._clock = clock
        self._server: asyncio.AbstractServer | None = None

    def handle_request(self, raw: bytes, *, peer_uid: int, now: int | None = None) -> bytes:
        current = int(self._clock()) if now is None else int(now)
        request_id = "unknown"
        try:
            if peer_uid not in self._allowed_peer_uids:
                raise ProtocolError("peer_not_allowed", "Unix-socket peer uid is not allowed")
            request = WireRequest.decode(raw)
            request_id = request.envelope.request_id
            self._authenticator.verify(request.envelope, request.body, now=current)
            self._replay_guard.admit(request.envelope, now=current)
            result = self._dispatch(request)
            return self._response(request.envelope.request_id, True, result)
        except AgentJobConflict:
            return self._response(request_id, False, {"error_code": "job_plan_conflict"})
        except (
            ContractError,
            AdapterValidationError,
            AgentJobStateError,
            KeyError,
            TypeError,
            ValueError,
        ) as exc:
            code = exc.code if isinstance(exc, ProtocolError) else "request_rejected"
            return self._response(request_id, False, {"error_code": code})
        except Exception:
            return self._response(request_id, False, {"error_code": "internal_error"})

    def _dispatch(self, request: WireRequest) -> dict[str, Any]:
        if request.envelope.method == "Health":
            if request.body:
                raise ProtocolError("invalid_request", "Health body must be empty")
            return self.health()
        if request.envelope.method == "Handshake":
            if set(request.body) != {"client_protocol_version"}:
                raise ProtocolError("invalid_request", "Handshake body is malformed")
            if request.body["client_protocol_version"] != PROTOCOL_VERSION:
                raise ProtocolError("unsupported_protocol", "client protocol does not match")
            return {"accepted": True, **self.health()}
        if request.envelope.method == "ValidateAction":
            if set(request.body) != {"plan"} or not isinstance(request.body["plan"], dict):
                raise ProtocolError("invalid_request", "ValidateAction body is malformed")
            validated = self._registry.validate_action(
                plan_payload=request.body["plan"],
                approved_plan_digest=request.envelope.plan_digest,
            )
            return validated.to_dict()
        if request.envelope.method == "RunAction":
            if set(request.body) not in ({"plan"}, {"network_approval_proof", "plan"}) or not isinstance(
                request.body["plan"], dict
            ):
                raise ProtocolError("invalid_request", "RunAction body is malformed")
            return self._run_action(request)
        if request.envelope.method == "JobStatus":
            if request.body:
                raise ProtocolError("invalid_request", "JobStatus body must be empty")
            return self._job_status(request)
        if request.envelope.method == "JobCancel":
            if request.body:
                raise ProtocolError("invalid_request", "JobCancel body must be empty")
            return self._cancel_job(request)
        if request.envelope.method == "JobReconcile":
            if request.body:
                raise ProtocolError("invalid_request", "JobReconcile body must be empty")
            return self._reconcile_job(request)
        if request.envelope.method == "PackagePlanInstall":
            return self._package_plan_install(request)
        if request.envelope.method == "PackageExecuteInstall":
            return self._package_execute_install(request)
        if request.envelope.method == "PackageStatus":
            return self._package_status(request)
        if request.envelope.method == "PackageReconcileInstall":
            return self._package_reconcile_install(request)
        if request.envelope.method == "PackageCancelBeforeCommit":
            return self._package_cancel_before_commit(request)
        raise ProtocolError("unknown_method", "method is not exposed by the host agent")

    def _require_package_client(self) -> PackageBrokerClient:
        if self._package_client is None:
            raise ProtocolError("package_broker_unavailable", "package broker is not configured")
        return self._package_client

    @staticmethod
    def _package_call(call: Any, *, request: WireRequest) -> dict[str, Any]:
        try:
            return call()
        except PackageBrokerUnknownOutcome:
            return {
                "error_code": "package_broker_outcome_unknown",
                "job_id": request.envelope.job_id,
                "status": "unknown",
            }
        except PackageBrokerRejected as exc:
            raise ProtocolError(exc.code, "package broker rejected the exact request") from exc
        except PackageBrokerUnavailable as exc:
            raise ProtocolError("package_broker_unavailable", "package broker is unavailable") from exc

    def _package_plan_install(self, request: WireRequest) -> dict[str, Any]:
        body = request.body
        if set(body) != {"continuation_work_item_id", "original_task_ref", "requested"}:
            raise ProtocolError("invalid_request", "package plan request is malformed")
        if body["continuation_work_item_id"] != request.envelope.job_id:
            raise ProtocolError("continuation_mismatch", "package plan continuation identity changed")
        raw_requested = body["requested"]
        if not isinstance(raw_requested, list) or not 1 <= len(raw_requested) <= 16:
            raise ProtocolError("invalid_request", "package plan request set is malformed")
        requested = tuple(PackageRef.from_payload(item) for item in raw_requested)
        client = self._require_package_client()
        return self._package_call(
            lambda: client.plan_install(
                requested=requested,
                original_task_ref=str(body["original_task_ref"]),
                continuation_work_item_id=str(body["continuation_work_item_id"]),
                actor_id=request.envelope.actor_id,
                own_id=request.envelope.own_id,
                idempotency_key=request.envelope.idempotency_key,
            ),
            request=request,
        )

    def _package_execute_install(self, request: WireRequest) -> dict[str, Any]:
        if set(request.body) != {"approval_proof", "plan_id"}:
            if set(request.body) == {"plan_id"} and request.envelope.approval_receipt_id is None:
                raise ProtocolError("approval_required", "package execution lacks exact approval binding")
            raise ProtocolError("invalid_request", "package execute request is malformed")
        envelope = request.envelope
        if envelope.approval_receipt_id is None or envelope.plan_digest == EMPTY_PLAN_DIGEST:
            raise ProtocolError("approval_required", "package execution lacks exact approval binding")
        proof = PackageApprovalProof.from_payload(request.body["approval_proof"])
        plan_id = str(request.body["plan_id"])
        if (
            proof.plan_id != plan_id
            or proof.plan_digest != envelope.plan_digest
            or proof.approval_receipt_id != envelope.approval_receipt_id
            or proof.continuation_work_item_id != envelope.job_id
            or proof.actor_user_id != envelope.actor_id
            or proof.actor_own_id != envelope.own_id
            or proof.execution_idempotency_key != envelope.idempotency_key
        ):
            raise ProtocolError("approval_binding_mismatch", "package approval proof binding changed")
        client = self._require_package_client()
        return self._package_call(
            lambda: client.execute_install(
                plan_id=plan_id,
                approved_plan_digest=envelope.plan_digest,
                approval_receipt_id=envelope.approval_receipt_id or "",
                approval_proof=proof,
                continuation_work_item_id=envelope.job_id,
                actor_id=envelope.actor_id,
                own_id=envelope.own_id,
                idempotency_key=envelope.idempotency_key,
            ),
            request=request,
        )

    def _package_status(self, request: WireRequest) -> dict[str, Any]:
        if set(request.body) != {"plan_id"}:
            raise ProtocolError("invalid_request", "package status request is malformed")
        envelope = request.envelope
        client = self._require_package_client()
        return self._package_call(
            lambda: client.status(
                plan_id=str(request.body["plan_id"]),
                continuation_work_item_id=envelope.job_id,
                actor_id=envelope.actor_id,
                own_id=envelope.own_id,
                idempotency_key=envelope.idempotency_key,
            ),
            request=request,
        )

    def _package_reconcile_install(self, request: WireRequest) -> dict[str, Any]:
        if set(request.body) != {"plan_id"} or request.envelope.plan_digest == EMPTY_PLAN_DIGEST:
            raise ProtocolError("invalid_request", "package reconciliation request is malformed")
        envelope = request.envelope
        if envelope.approval_receipt_id is not None:
            raise ProtocolError("invalid_request", "package reconciliation cannot carry approval")
        client = self._require_package_client()
        return self._package_call(
            lambda: client.reconcile_after_restart(
                plan_id=str(request.body["plan_id"]),
                plan_digest=envelope.plan_digest,
                continuation_work_item_id=envelope.job_id,
                actor_id=envelope.actor_id,
                own_id=envelope.own_id,
                idempotency_key=envelope.idempotency_key,
            ),
            request=request,
        )

    def _package_cancel_before_commit(self, request: WireRequest) -> dict[str, Any]:
        if set(request.body) != {"plan_id"}:
            raise ProtocolError("invalid_request", "package cancellation request is malformed")
        envelope = request.envelope
        client = self._require_package_client()
        return self._package_call(
            lambda: client.cancel_before_commit(
                plan_id=str(request.body["plan_id"]),
                continuation_work_item_id=envelope.job_id,
                actor_id=envelope.actor_id,
                own_id=envelope.own_id,
                idempotency_key=envelope.idempotency_key,
            ),
            request=request,
        )

    def _require_execution(self) -> tuple[ProcessRunner, AgentJobStore, Path]:
        if self._runner is None or self._job_store is None or self._job_root is None:
            raise ProtocolError("execution_disabled", "host action execution is not configured")
        return self._runner, self._job_store, self._job_root

    @staticmethod
    def _assert_envelope_matches_plan(request: WireRequest, plan: Any) -> None:
        envelope = request.envelope
        if (
            plan.actor_user_id != envelope.actor_id
            or plan.actor_own_id != envelope.own_id
            or plan.host_agent_id != envelope.agent_id
            or plan.idempotency_key != envelope.idempotency_key
            or plan.digest != envelope.plan_digest
        ):
            raise ProtocolError("plan_envelope_mismatch", "signed envelope does not name the plan")

    def _network_approval_for_request(
        self,
        request: WireRequest,
        plan: Any,
        *,
        now: int,
        existing_claim_only: bool = False,
    ) -> NetworkApprovalProof | None:
        snapshot_payload = plan.target_snapshot
        approval_required = False
        if isinstance(snapshot_payload, dict):
            approval_required = NetworkTargetSnapshot.from_payload(snapshot_payload).approval_required
        raw_proof = request.body.get("network_approval_proof")
        if not approval_required:
            if raw_proof is not None or request.envelope.approval_receipt_id is not None:
                raise ProtocolError(
                    "network_approval_unexpected",
                    "private action cannot carry a public-network approval",
                )
            return None
        if raw_proof is None or request.envelope.approval_receipt_id is None:
            raise ProtocolError(
                "network_approval_required",
                "public-network action lacks an exact approval proof",
            )
        verifier = self._network_approval_verifier
        ledger = self._network_approval_ledger
        if verifier is None or ledger is None:
            raise ProtocolError(
                "network_approval_unavailable",
                "public-network approval verification is unavailable",
            )
        try:
            proof = NetworkApprovalProof.from_payload(raw_proof)
            if not existing_claim_only:
                verifier.verify(proof, now=now)
            approval_payload_digest = self._network_approval_payload_digest(request, plan)
            assert_network_approval_binding(
                proof,
                host_agent_id=self.agent_id,
                approval_receipt_id=request.envelope.approval_receipt_id,
                approval_payload_digest=approval_payload_digest,
                plan_id=plan.plan_id,
                plan_digest=request.envelope.plan_digest,
                plan_created_at=plan.created_at,
                plan_expires_at=plan.expires_at,
                job_id=request.envelope.job_id,
                execution_idempotency_key=request.envelope.idempotency_key,
                actor_user_id=request.envelope.actor_id,
                actor_own_id=request.envelope.own_id,
            )
            if existing_claim_only:
                ledger.assert_claimed(proof)
            else:
                ledger.claim(proof, now=now)
        except NetworkApprovalError as exc:
            raise ProtocolError(exc.code, "public-network approval was rejected") from exc
        return proof

    @staticmethod
    def _network_approval_payload_digest(request: WireRequest, plan: Any) -> str:
        payload = {
            "job_id": request.envelope.job_id,
            "plan": plan.to_payload(),
            "plan_digest": plan.digest,
        }
        return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()

    def _recheck_network_approval(
        self,
        request: WireRequest,
        plan: Any,
        proof: NetworkApprovalProof,
    ) -> None:
        verifier = self._network_approval_verifier
        ledger = self._network_approval_ledger
        if verifier is None or ledger is None:
            raise NetworkApprovalError("network_approval_unavailable")
        verifier.verify(proof, now=int(self._clock()))
        assert_network_approval_binding(
            proof,
            host_agent_id=self.agent_id,
            approval_receipt_id=request.envelope.approval_receipt_id,
            approval_payload_digest=self._network_approval_payload_digest(request, plan),
            plan_id=plan.plan_id,
            plan_digest=request.envelope.plan_digest,
            plan_created_at=plan.created_at,
            plan_expires_at=plan.expires_at,
            job_id=request.envelope.job_id,
            execution_idempotency_key=request.envelope.idempotency_key,
            actor_user_id=request.envelope.actor_id,
            actor_own_id=request.envelope.own_id,
        )
        ledger.assert_claimed(proof)

    def _run_action(self, request: WireRequest) -> dict[str, Any]:
        runner, jobs, root = self._require_execution()
        existing = jobs.get(request.envelope.job_id)
        if existing is not None:
            # An exact authenticated retry is a read of the already-admitted
            # immutable job identity, not a new public-network effect. Return
            # its durable result even after the original plan/proof expires or
            # the live adapter policy changes; JobStatus has the same boundary.
            self._assert_job_actor(request, existing)
            existing_plan = HostActionPlan.from_payload(request.body["plan"])
            self._assert_envelope_matches_plan(request, existing_plan)
            self._network_approval_for_request(
                request,
                existing_plan,
                now=int(self._clock()),
                existing_claim_only=True,
            )
            existing_result = existing.get("result")
            if isinstance(existing_result, dict):
                return existing_result
            return {"job_id": existing["job_id"], "status": existing["status"]}
        validated = self._registry.validate_action(
            plan_payload=request.body["plan"],
            approved_plan_digest=request.envelope.plan_digest,
        )
        self._assert_envelope_matches_plan(request, validated.plan)
        network_approval = self._network_approval_for_request(
            request,
            validated.plan,
            now=int(self._clock()),
        )
        record, created = jobs.admit(
            job_id=request.envelope.job_id,
            idempotency_key=request.envelope.idempotency_key,
            plan_digest=request.envelope.plan_digest,
            actor_id=request.envelope.actor_id,
            own_id=request.envelope.own_id,
        )
        if not created:
            # A concurrent exact request may have admitted the same identity
            # after the pre-check. It passed the proof boundary independently;
            # never run a second process for the same durable job.
            result = record.get("result")
            if isinstance(result, dict):
                return result
            return {"job_id": record["job_id"], "status": record["status"]}
        if not self._execution_slots.acquire(blocking=False):
            return jobs.transition(
                request.envelope.job_id,
                expected=("admitted",),
                status="failed",
                result={"error_code": "agent_busy", "job_id": request.envelope.job_id, "status": "failed"},
            )["result"]
        try:
            workspace_path = _prepare_workspace(root, request.envelope.job_id)
            workspace = WorkspaceGrant(
                job_id=request.envelope.job_id,
                actor_own_id=request.envelope.own_id,
                workspace_root=str(workspace_path),
                grants=validated.plan.workspace_grants,
            )
            # Reload/re-normalize against the root-owned agent policy at the
            # final native seam. A forged backend plan or an operator revoke
            # must close before the process boundary can be crossed.
            try:
                self._registry.assert_target_policy_current(validated.plan)
                if network_approval is not None:
                    self._recheck_network_approval(request, validated.plan, network_approval)
            except AdapterValidationError:
                policy_response = {
                    "error_code": "target_policy_changed",
                    "job_id": request.envelope.job_id,
                    "status": "failed",
                }
                jobs.transition(
                    request.envelope.job_id,
                    expected=("admitted",),
                    status="failed",
                    result=policy_response,
                )
                return policy_response
            except NetworkApprovalError as exc:
                approval_response = {
                    "error_code": exc.code,
                    "job_id": request.envelope.job_id,
                    "status": "failed",
                }
                jobs.transition(
                    request.envelope.job_id,
                    expected=("admitted",),
                    status="failed",
                    result=approval_response,
                )
                raise ProtocolError(exc.code, "public-network approval changed before launch") from exc
            jobs.transition(
                request.envelope.job_id,
                expected=("admitted",),
                status="running",
            )
            try:
                process = runner.run(
                    job_id=request.envelope.job_id,
                    plan=validated.plan,
                    executable=validated.executable,
                    execution=validated.execution,
                    workspace=workspace,
                    budgets=self._budgets,
                )
            except RunnerUnavailable:
                response: dict[str, Any] = {
                    "error_code": "runner_unavailable",
                    "job_id": request.envelope.job_id,
                    "status": "failed",
                }
                jobs.transition(
                    request.envelope.job_id,
                    expected=("running",),
                    status="failed",
                    result=response,
                )
                return response
            current = jobs.get(request.envelope.job_id)
            if current is not None and current.get("status") != "running":
                existing_result = current.get("result")
                return (
                    existing_result
                    if isinstance(existing_result, dict)
                    else {
                        "job_id": request.envelope.job_id,
                        "status": current["status"],
                    }
                )
            parsed, evidence, paths = _capture_and_parse(
                workspace_path,
                validated.implementation,
                validated.plan,
                process,
            )
            complete = (
                parsed.parser_status.value == "complete"
                and parsed.coverage.grade.value == "complete"
                and process.outcome == "completed"
                and process.exit_code == 0
                and not process.output_truncated
            )
            postconditions = ("process_terminal", "parser_complete", "coverage_complete") if complete else ()
            receipt = self._receipt_signer.sign(
                build_receipt(
                    job_id=request.envelope.job_id,
                    plan=validated.plan,
                    host_agent_version=self._build_id,
                    executable_attestation=validated.executable,
                    execution=validated.execution,
                    result=process,
                    evidence=evidence,
                    parsed_result_digest=parsed.digest,
                    postconditions=postconditions,
                )
            )
            receipt_ref, receipt_path = _write_evidence(
                workspace_path / "evidence",
                canonical_json_bytes(receipt.to_payload()),
                "application/json",
                "receipt.json",
            )
            paths[receipt_ref.evidence_id] = receipt_path
            outcome_status = {
                EffectOutcome.SUCCEEDED: "completed",
                EffectOutcome.PARTIAL: "partial",
                EffectOutcome.FAILED: "failed",
                EffectOutcome.CANCELLED: "cancelled",
                EffectOutcome.UNKNOWN: "unknown",
            }[receipt.effect_outcome]
            response = {
                "evidence_paths": paths,
                "job_id": request.envelope.job_id,
                "receipt": receipt.to_payload(),
                "receipt_path": receipt_path,
                "result": project_action_result(parsed),
                "status": outcome_status,
            }
            jobs.transition(
                request.envelope.job_id,
                expected=("running",),
                status=outcome_status,
                result=response,
            )
            return response
        except Exception:
            current = jobs.get(request.envelope.job_id)
            if current is not None and current.get("status") == "running":
                unknown = {
                    "error_code": "agent_failure_after_admission",
                    "job_id": request.envelope.job_id,
                    "status": "unknown",
                }
                jobs.transition(
                    request.envelope.job_id,
                    expected=("running",),
                    status="unknown",
                    result=unknown,
                )
                return unknown
            if current is not None and isinstance(current.get("result"), dict):
                return current["result"]
            raise
        finally:
            self._execution_slots.release()

    @staticmethod
    def _assert_job_actor(request: WireRequest, record: dict[str, Any]) -> None:
        envelope = request.envelope
        if (
            record.get("actor_id") != envelope.actor_id
            or record.get("own_id") != envelope.own_id
            or record.get("idempotency_key") != envelope.idempotency_key
            or record.get("plan_digest") != envelope.plan_digest
        ):
            raise ProtocolError("job_identity_mismatch", "job does not belong to the signed actor/plan")

    def _job_status(self, request: WireRequest) -> dict[str, Any]:
        _runner, jobs, _root = self._require_execution()
        record = jobs.get(request.envelope.job_id)
        if record is None:
            raise ProtocolError("job_not_found", "host action job does not exist")
        self._assert_job_actor(request, record)
        result = record.get("result")
        return (
            result
            if isinstance(result, dict)
            else {
                "job_id": request.envelope.job_id,
                "status": record["status"],
            }
        )

    def _cancel_job(self, request: WireRequest) -> dict[str, Any]:
        runner, jobs, _root = self._require_execution()
        record = jobs.get(request.envelope.job_id)
        if record is None:
            raise ProtocolError("job_not_found", "host action job does not exist")
        self._assert_job_actor(request, record)
        current_status = str(record["status"])
        if current_status not in {"running", "unknown"}:
            return {"job_id": request.envelope.job_id, "status": record["status"], "cancelled": False}
        observed = runner.cancel(request.envelope.job_id)
        status = "cancelled" if observed else "unknown"
        response = {"cancelled": observed, "job_id": request.envelope.job_id, "status": status}
        try:
            jobs.transition(
                request.envelope.job_id,
                expected=(current_status,),
                status=status,
                result=response,
            )
        except AgentJobStateError:
            return self._job_status(request)
        return response

    def _reconcile_job(self, request: WireRequest) -> dict[str, Any]:
        runner, jobs, _root = self._require_execution()
        record = jobs.get(request.envelope.job_id)
        if record is None:
            raise ProtocolError("job_not_found", "host action job does not exist")
        self._assert_job_actor(request, record)
        result = record.get("result")
        if record["status"] in {"completed", "partial", "failed", "cancelled"} and isinstance(result, dict):
            # Terminal responses already commit the signed receipt and evidence
            # references to the durable ledger.  Returning that exact object is
            # the only restart-safe way to settle the backend projection.
            return result
        ledger_status = str(record["status"])
        error_code = (
            str(result.get("error_code"))
            if isinstance(result, dict) and isinstance(result.get("error_code"), str)
            else "agent_outcome_unknown"
        )
        return {
            "error_code": error_code,
            "job_id": request.envelope.job_id,
            "ledger_status": ledger_status,
            "reconciliation_required": ledger_status == "unknown",
            # A surviving/terminated systemd unit cannot recreate stdout,
            # parser evidence, or a receipt lost with the old daemon.  Preserve
            # that uncertainty instead of guessing an effect outcome.
            "status": ledger_status if ledger_status in {"admitted", "running"} else "unknown",
            "systemd": runner.reconcile(request.envelope.job_id),
        }

    def health(self) -> dict[str, Any]:
        desktop = "no_graphical_session"
        if os.environ.get("WAYLAND_DISPLAY") or os.environ.get("DISPLAY"):
            desktop = "launch_only"
        return {
            "agent_id": self.agent_id,
            "build_id": self._build_id,
            "protocol_versions": [PROTOCOL_VERSION],
            "os": platform.system().casefold(),
            "os_release": platform.release(),
            "architecture": platform.machine(),
            "user_uid": os.geteuid(),
            "systemd_user_manager": Path("/run/user", str(os.geteuid()), "bus").exists(),
            "desktop_capability": desktop,
            "dbus_available": bool(os.environ.get("DBUS_SESSION_BUS_ADDRESS")),
            "wayland_available": bool(os.environ.get("WAYLAND_DISPLAY")),
            "x11_available": bool(os.environ.get("DISPLAY")),
            "adapter_catalog_digest": self._registry.catalog_digest(),
            "network_policy_digest": self._registry.network_policy_digest(),
            "network_approval_public_key_digest": (
                self._network_approval_verifier.public_key_digest
                if self._network_approval_verifier is not None
                else None
            ),
            "inventory": [entry.to_dict() for entry in self._inventory.snapshot()],
            "running_job_count": self._job_store.running_count() if self._job_store is not None else 0,
            "package_broker": "configured" if self._package_client is not None else "unavailable",
            "clock_unix_sec": int(self._clock()),
        }

    def _response(self, request_id: str, ok: bool, result: dict[str, Any]) -> bytes:
        body = {
            "agent_id": self.agent_id,
            "ok": ok,
            "protocol_version": PROTOCOL_VERSION,
            "request_id": request_id,
            "result": result,
        }
        signature = self._authenticator.sign_bytes(
            canonical_json(body), domain=b"friday-host-agent-response-v1"
        )
        return canonical_json({**body, "signature": signature}, max_bytes=MAX_WIRE_BYTES)

    async def serve(self, socket_path: str | Path) -> None:
        if os.geteuid() == 0:
            raise RuntimeError("friday-host-agent must never run as root")
        path = Path(socket_path)
        if not path.is_absolute() or path.is_symlink():
            raise ValueError("agent socket path must be absolute and non-symlinked")
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        parent = path.parent
        observed_parent = parent.lstat()
        if (
            parent.is_symlink()
            or str(parent.resolve(strict=True)) != str(parent)
            or observed_parent.st_uid != os.geteuid()
        ):
            raise ValueError("agent socket directory is not owned canonical state")
        os.chmod(path.parent, 0o700)
        _retire_stale_socket(path)
        self._server = await asyncio.start_unix_server(
            self._serve_client, path=str(path), limit=MAX_WIRE_BYTES
        )
        os.chmod(path, 0o600)
        created_socket = path.lstat()
        try:
            async with self._server:
                await self._server.serve_forever()
        finally:
            current_socket: os.stat_result | None
            try:
                current_socket = path.lstat()
            except OSError:
                current_socket = None
            if current_socket is not None and (
                current_socket.st_dev,
                current_socket.st_ino,
            ) == (created_socket.st_dev, created_socket.st_ino):
                path.unlink()

    async def _serve_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            peer_uid = _peer_uid(writer)
            try:
                raw = await asyncio.wait_for(reader.readline(), timeout=self._client_timeout_sec)
            except (TimeoutError, asyncio.LimitOverrunError, ValueError):
                raw = b""
            if not raw or len(raw) > MAX_WIRE_BYTES or not raw.endswith(b"\n"):
                response = self._response("unknown", False, {"error_code": "invalid_framing"})
            else:
                response = await asyncio.to_thread(
                    self.handle_request,
                    raw[:-1],
                    peer_uid=peer_uid,
                )
            writer.write(response + b"\n")
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()


def _peer_uid(writer: asyncio.StreamWriter) -> int:
    transport_socket = writer.get_extra_info("socket")
    if transport_socket is None or not hasattr(socket, "SO_PEERCRED"):
        raise ProtocolError("peer_unknown", "Unix peer credentials are unavailable")
    credentials = transport_socket.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
    _pid, uid, _gid = struct.unpack("3i", credentials)
    return uid


def _retire_stale_socket(path: Path) -> None:
    try:
        observed = path.lstat()
    except FileNotFoundError:
        return
    if (
        not stat.S_ISSOCK(observed.st_mode)
        or observed.st_uid != os.geteuid()
        or observed.st_mode & (stat.S_IRWXG | stat.S_IRWXO)
    ):
        raise ValueError("agent socket path contains unsafe existing state")
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        probe.settimeout(0.2)
        probe.connect(str(path))
    except ConnectionRefusedError:
        path.unlink()
    except OSError as exc:
        raise ValueError("existing agent socket state could not be proven stale") from exc
    else:
        raise ValueError("another host agent is already listening")
    finally:
        probe.close()


def response_body_hash(response: bytes) -> str:
    """Content-free correlation helper for daemon diagnostics."""

    return hashlib.sha256(response).hexdigest()


def _trusted_job_root(path: Path) -> Path:
    if (
        not path.is_absolute()
        or path.is_symlink()
        or str(path.resolve(strict=True)) != str(path)
        or not path.is_dir()
    ):
        raise ValueError("host-agent job root must be a canonical directory")
    observed = path.lstat()
    if observed.st_uid != os.geteuid() or observed.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise ValueError("host-agent job root has unsafe ownership or permissions")
    return path


def _prepare_workspace(root: Path, job_id: str) -> Path:
    workspace = root / job_id
    with contextlib.suppress(FileExistsError):
        workspace.mkdir(mode=0o700)
    if workspace.is_symlink() or workspace.parent != root or workspace.resolve(strict=True).parent != root:
        raise ValueError("host job workspace escapes its configured root")
    observed = workspace.lstat()
    if not stat.S_ISDIR(observed.st_mode) or observed.st_uid != os.geteuid():
        raise ValueError("host job workspace ownership is invalid")
    os.chmod(workspace, 0o700)
    for name in ("input", "work", "output", "evidence"):
        child = workspace / name
        with contextlib.suppress(FileExistsError):
            child.mkdir(mode=0o700)
        if child.is_symlink() or child.resolve(strict=True).parent != workspace:
            raise ValueError("host job workspace contains an unsafe directory")
        child_stat = child.lstat()
        if not stat.S_ISDIR(child_stat.st_mode) or child_stat.st_uid != os.geteuid():
            raise ValueError("host job workspace directory ownership is invalid")
        os.chmod(child, 0o700)
    return workspace


def _write_evidence(directory: Path, payload: bytes, media_type: str, suffix: str) -> tuple[EvidenceRef, str]:
    evidence_id = f"evidence_{uuid.uuid4().hex}"
    name = f"{evidence_id}.{suffix}"
    destination = directory / name
    descriptor = os.open(
        destination,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        os.close(descriptor)
    digest = hashlib.sha256(payload).hexdigest()
    return EvidenceRef(evidence_id, digest, len(payload), media_type), f"evidence/{name}"


def _capture_and_parse(
    workspace: Path,
    adapter: Any,
    plan: Any,
    process: Any,
) -> tuple[ParsedActionResult, tuple[EvidenceRef, ...], dict[str, str]]:
    evidence_dir = workspace / "evidence"
    if isinstance(adapter, NmapAdapter):
        raw_ref, raw_path = _write_evidence(evidence_dir, process.stdout, "application/xml", "xml")
        snapshot_payload = plan.target_snapshot
        if snapshot_payload is None:
            raise ValueError("nmap plan lacks a target snapshot")
        parsed = adapter.parse_xml(
            process.stdout,
            target_snapshot=NetworkTargetSnapshot.from_payload(snapshot_payload),
            exit_code=process.exit_code,
            timed_out=process.timed_out,
            truncated=process.output_truncated,
            evidence=(raw_ref,),
        )
    elif isinstance(adapter, JqAdapter):
        raw_ref, raw_path = _write_evidence(evidence_dir, process.stdout, "application/json", "json")
        parsed = adapter.parse_json(
            process.stdout,
            exit_code=process.exit_code,
            truncated=process.output_truncated,
            evidence=(raw_ref,),
        )
    else:
        raise ValueError("adapter has no reviewed result parser")
    parsed_payload = canonical_json_bytes(parsed.to_payload())
    parsed_ref, parsed_path = _write_evidence(
        evidence_dir,
        parsed_payload,
        "application/json",
        "result.json",
    )
    references = [raw_ref, parsed_ref]
    paths = {raw_ref.evidence_id: raw_path, parsed_ref.evidence_id: parsed_path}
    if process.stderr:
        stderr_ref, stderr_path = _write_evidence(
            evidence_dir,
            process.stderr,
            "text/plain",
            "stderr.txt",
        )
        references.append(stderr_ref)
        paths[stderr_ref.evidence_id] = stderr_path
    return parsed, tuple(references), paths


__all__ = ["HostAgentDaemon", "response_body_hash"]
