"""Versioned, authenticated root UDS server with a closed package API."""

from __future__ import annotations

import asyncio
import hmac
import os
import re
import secrets
import socket
import stat
import struct
import time
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import Any

from friday.host_control.contracts import (
    MAX_WIRE_BYTES,
    PROTOCOL_VERSION,
    ContractError,
    WireRequest,
    canonical_digest,
)

from .approval import ApprovalProofError, PackageApprovalProof, PackageApprovalVerifier
from .apt_backend import (
    AptBackend,
    AptBackendError,
    AptBackendHealth,
    AptExecutionResult,
    AptReconciliationResult,
)
from .authentication import (
    RECEIPT_DOMAIN,
    RECONCILIATION_DOMAIN,
    RESPONSE_DOMAIN,
    BrokerAuthenticationError,
    BrokerAuthenticator,
    ReplayLedger,
)
from .contracts import (
    BROKER_PLAN_SCHEMA_VERSION,
    BROKER_RECEIPT_SCHEMA_VERSION,
    BROKER_RECONCILIATION_SCHEMA_VERSION,
    EMPTY_PLAN_DIGEST,
    AptInstallPlan,
    BrokerContractError,
    BrokerWireResponse,
    PackagePostconditionState,
    PackageReconciliationReceipt,
    PackageRef,
    PackageTransactionReceipt,
    TransactionOutcome,
)
from .policy import BrokerPolicy
from .store import (
    BrokerStore,
    BrokerStoreError,
    PlanStatus,
    StoreConflict,
    StoredPlan,
    StoreStateError,
)

_PLAN_ID = re.compile(r"^aptplan_[0-9a-f]{16,64}$")
_METHODS = frozenset(
    {
        "CancelBeforeCommit",
        "ExecuteInstall",
        "Health",
        "PlanInstall",
        "ReconcileAfterRestart",
        "Status",
    }
)


class BrokerRequestError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class PackageBrokerDaemon:
    """Closed dispatcher.  There is intentionally no command or repository API."""

    def __init__(
        self,
        *,
        policy: BrokerPolicy,
        authenticator: BrokerAuthenticator,
        replay_ledger: ReplayLedger,
        store: BrokerStore,
        backend: AptBackend,
        approval_verifier: PackageApprovalVerifier,
        build_id: str = "development",
        clock: Callable[[], int] | None = None,
        max_concurrency: int = 4,
    ) -> None:
        if policy.broker_id != authenticator.broker_id:
            raise ValueError("broker authentication and policy identities differ")
        if not authenticator.can_sign_responses:
            raise ValueError("broker daemon requires a private response-signing key")
        if not isinstance(build_id, str) or not build_id or len(build_id) > 160:
            raise ValueError("broker build identity is invalid")
        if not 1 <= max_concurrency <= 16:
            raise ValueError("broker concurrency limit is invalid")
        self.policy = policy
        self._authenticator = authenticator
        self._replay_ledger = replay_ledger
        self._store = store
        self._backend = backend
        self._approval_verifier = approval_verifier
        self._build_id = build_id
        self._clock = clock or (lambda: int(time.time()))
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._server: asyncio.AbstractServer | None = None
        self._client_timeout_sec = 10.0
        self._accepting_requests = True
        self._active_clients: set[asyncio.Task[Any]] = set()
        self._clients_drained = asyncio.Event()
        self._clients_drained.set()

    def handle_request(self, raw: bytes, *, peer_uid: int, now: int | None = None) -> bytes:
        request_id = "unknown"
        try:
            if peer_uid not in self.policy.allowed_peer_uids:
                raise BrokerAuthenticationError("peer_not_allowed")
            request = WireRequest.decode(raw)
            if request.encode() != raw:
                raise BrokerRequestError("noncanonical_request")
            request_id = request.envelope.request_id
            current = self._now(now)
            self._authenticator.verify(request.envelope, request.body, now=current)
            self._replay_ledger.admit(request.envelope, now=current)
            if request.envelope.method not in _METHODS:
                raise BrokerRequestError("unknown_method")
            result = self._dispatch(request, now_override=now)
            return self._response(request_id, ok=True, result=result)
        except BrokerAuthenticationError as exc:
            return self._response(request_id, ok=False, result={"error_code": exc.code})
        except ApprovalProofError as exc:
            return self._response(request_id, ok=False, result={"error_code": exc.code})
        except BrokerRequestError as exc:
            return self._response(request_id, ok=False, result={"error_code": exc.code})
        except StoreConflict as exc:
            return self._response(request_id, ok=False, result={"error_code": exc.code})
        except (StoreStateError, BrokerStoreError) as exc:
            return self._response(request_id, ok=False, result={"error_code": exc.code})
        except AptBackendError as exc:
            return self._response(request_id, ok=False, result={"error_code": exc.code})
        except (BrokerContractError, ContractError, KeyError, TypeError, ValueError):
            return self._response(request_id, ok=False, result={"error_code": "request_rejected"})
        except Exception:
            return self._response(request_id, ok=False, result={"error_code": "broker_internal_error"})

    def _dispatch(self, request: WireRequest, *, now_override: int | None) -> dict[str, Any]:
        method = request.envelope.method
        if method == "Health":
            self._require_unapproved(request)
            if request.body:
                raise BrokerRequestError("invalid_request")
            health = self._backend.health()
            if not isinstance(health, AptBackendHealth):
                raise BrokerRequestError("backend_contract_error")
            return {
                "broker_id": self.policy.broker_id,
                "build_id": self._build_id,
                "methods": sorted(_METHODS),
                "package_backend": health.to_payload(),
                "protocol_versions": [PROTOCOL_VERSION],
            }
        if method == "PlanInstall":
            return self._plan_install(request, now_override=now_override)
        if method == "ExecuteInstall":
            return self._execute_install(request, now_override=now_override)
        if method == "Status":
            return self._status(request)
        if method == "ReconcileAfterRestart":
            return self._reconcile_after_restart(request, now_override=now_override)
        if method == "CancelBeforeCommit":
            return self._cancel(request, now_override=now_override)
        raise BrokerRequestError("unknown_method")

    def _plan_install(self, request: WireRequest, *, now_override: int | None) -> dict[str, Any]:
        self._require_unapproved(request)
        expected = {"continuation_work_item_id", "original_task_ref", "requested"}
        if set(request.body) != expected or not isinstance(request.body["requested"], list):
            raise BrokerRequestError("invalid_request")
        if not 1 <= len(request.body["requested"]) <= 16:
            raise BrokerRequestError("invalid_request")
        requested = tuple(PackageRef.from_payload(item) for item in request.body["requested"])
        self.policy.authorize_requested(requested)
        if request.envelope.job_id != request.body["continuation_work_item_id"]:
            raise BrokerRequestError("continuation_mismatch")
        request_digest = canonical_digest(
            {
                "actor_own_id": request.envelope.own_id,
                "actor_user_id": request.envelope.actor_id,
                "body": request.body,
                "broker_id": self.policy.broker_id,
                "method": "PlanInstall",
            }
        )
        existing = self._store.idempotent_plan(
            idempotency_key=request.envelope.idempotency_key,
            request_digest=request_digest,
            actor_user_id=request.envelope.actor_id,
            actor_own_id=request.envelope.own_id,
        )
        if existing is not None:
            return self._record_payload(existing, include_plan=True, idempotent=True)

        transaction = self._backend.plan(requested)
        self.policy.authorize(transaction)
        current = self._now(now_override)
        if request.envelope.expires_at <= current:
            raise BrokerRequestError("request_expired")
        plan = AptInstallPlan(
            schema_version=BROKER_PLAN_SCHEMA_VERSION,
            plan_id=f"aptplan_{secrets.token_hex(16)}",
            broker_id=self.policy.broker_id,
            actor_user_id=request.envelope.actor_id,
            actor_own_id=request.envelope.own_id,
            original_task_ref=request.body["original_task_ref"],
            continuation_work_item_id=request.body["continuation_work_item_id"],
            transaction=transaction,
            created_at=current,
            expires_at=current + self.policy.plan_ttl_sec,
        )
        saved = self._store.save_plan(
            plan,
            request_digest=request_digest,
            idempotency_key=request.envelope.idempotency_key,
        )
        return self._record_payload(saved, include_plan=True, idempotent=False)

    def _execute_install(self, request: WireRequest, *, now_override: int | None) -> dict[str, Any]:
        if set(request.body) != {"approval_proof", "plan_id"}:
            if set(request.body) == {"plan_id"}:
                if request.envelope.approval_receipt_id is None:
                    raise BrokerRequestError("approval_required")
                raise BrokerRequestError("approval_proof_required")
            raise BrokerRequestError("invalid_request")
        plan_id = self._plan_id(request.body["plan_id"])
        proof = PackageApprovalProof.from_payload(request.body["approval_proof"])
        current = self._now(now_override)
        approval_id = request.envelope.approval_receipt_id
        if approval_id is None:
            raise BrokerRequestError("approval_required")
        if (
            proof.broker_id != self.policy.broker_id
            or proof.approval_receipt_id != approval_id
            or proof.plan_id != plan_id
            or not hmac.compare_digest(proof.plan_digest, request.envelope.plan_digest)
            or proof.actor_user_id != request.envelope.actor_id
            or proof.actor_own_id != request.envelope.own_id
            or proof.continuation_work_item_id != request.envelope.job_id
            or proof.execution_idempotency_key != request.envelope.idempotency_key
        ):
            raise ApprovalProofError("approval_binding_mismatch")
        record = self._store.get(
            plan_id,
            actor_user_id=request.envelope.actor_id,
            actor_own_id=request.envelope.own_id,
        )
        if request.envelope.job_id != record.plan.continuation_work_item_id:
            raise BrokerRequestError("continuation_mismatch")
        if not hmac.compare_digest(request.envelope.plan_digest, record.plan.digest):
            raise BrokerRequestError("approved_plan_mismatch")
        if record.execution_idempotency_key is not None:
            # This is an exact read of an immutable execution claim whose proof
            # was verified before the first effect. Preserve the durable result
            # after the short proof TTL instead of turning a lost response into
            # a false new authorization attempt.
            if (
                record.execution_idempotency_key != request.envelope.idempotency_key
                or record.approval_receipt_id != approval_id
                or record.approval_proof_id != proof.proof_id
                or record.approval_proof_digest != proof.digest
            ):
                raise StoreConflict("execution_idempotency_conflict")
            return self._record_payload(record, include_plan=False, idempotent=True)
        if record.status is not PlanStatus.PLANNED:
            raise StoreStateError("plan_not_executable")

        self._approval_verifier.verify(proof, now=current)
        if proof.issued_at < record.plan.created_at or proof.expires_at > record.plan.expires_at:
            raise ApprovalProofError("approval_binding_mismatch")
        expected_approval_payload_digest = canonical_digest(
            {
                "job_id": record.plan.continuation_work_item_id,
                "package_plan": record.plan.to_payload(),
                "plan_digest": record.plan.digest,
            }
        )
        if not hmac.compare_digest(
            proof.approval_payload_digest,
            expected_approval_payload_digest,
        ):
            raise ApprovalProofError("approval_binding_mismatch")
        fresh = self._backend.plan(record.plan.transaction.requested)
        self.policy.authorize(fresh)
        if not hmac.compare_digest(fresh.digest, record.plan.transaction.digest):
            raise BrokerRequestError("plan_drift")
        claim_time = self._now(now_override)
        self._approval_verifier.verify(proof, now=claim_time)
        if request.envelope.expires_at <= claim_time:
            raise BrokerRequestError("request_expired")
        claim = self._store.claim_execution(
            plan_id,
            actor_user_id=request.envelope.actor_id,
            actor_own_id=request.envelope.own_id,
            plan_digest=request.envelope.plan_digest,
            execution_idempotency_key=request.envelope.idempotency_key,
            transaction_id=f"apttxn_{secrets.token_hex(16)}",
            approval_receipt_id=approval_id,
            approval_proof_id=proof.proof_id,
            approval_proof_digest=proof.digest,
            now=claim_time,
        )
        if not claim.should_execute:
            return self._record_payload(claim.record, include_plan=False, idempotent=True)

        execution_started = self._now(now_override)
        try:
            result = self._backend.execute_exact(
                claim.record.plan.transaction, deadline=request.envelope.expires_at
            )
            if not isinstance(result, AptExecutionResult):
                raise TypeError("package backend returned an invalid result")
        except Exception:
            result = AptExecutionResult(
                outcome=TransactionOutcome.UNKNOWN,
                effect_boundary_crossed=True,
                started_at=execution_started,
                finished_at=self._now(now_override),
                exit_code=None,
                lock_state="unknown",
                before=(),
                after=(),
                error_code="backend_outcome_unknown",
                manager_version="unavailable",
            )
        receipt = PackageTransactionReceipt(
            schema_version=BROKER_RECEIPT_SCHEMA_VERSION,
            protocol_version=PROTOCOL_VERSION,
            broker_id=self.policy.broker_id,
            broker_build_id=self._build_id,
            package_manager="apt",
            package_manager_version=result.manager_version,
            transaction_id=claim.record.transaction_id or "",
            plan_id=plan_id,
            approved_plan_digest=claim.record.plan.digest,
            executed_transaction_digest=(
                result.observed_transaction_digest or claim.record.plan.transaction.digest
            ),
            approval_receipt_id=approval_id,
            idempotency_key=request.envelope.idempotency_key,
            outcome=result.outcome,
            effect_boundary_crossed=result.effect_boundary_crossed,
            started_at=result.started_at,
            finished_at=result.finished_at,
            exit_code=result.exit_code,
            lock_state=result.lock_state,
            before=result.before,
            after=result.after,
            output_capture_status=result.output_capture_status,
            stdout_sha256=result.stdout_sha256,
            stdout_size_bytes=result.stdout_size_bytes,
            stderr_sha256=result.stderr_sha256,
            stderr_size_bytes=result.stderr_size_bytes,
            output_truncated=result.output_truncated,
            reboot_required=result.reboot_required,
            stdout_total_size_bytes=result.stdout_total_size_bytes,
            stderr_total_size_bytes=result.stderr_total_size_bytes,
            stdout_total_size_complete=result.stdout_total_size_complete,
            stderr_total_size_complete=result.stderr_total_size_complete,
            evidence_refs=result.evidence_refs,
            service_unit_observation_status=result.service_unit_observation_status,
            service_unit_observations=result.service_unit_observations,
            error_code=result.error_code,
            signature="",
        )
        signature = self._authenticator.sign_bytes(
            receipt.canonical_bytes_for_signing(),
            domain=RECEIPT_DOMAIN,
        )
        receipt = receipt.with_signature(signature)
        status = _terminal_status(result.outcome)
        finished = self._store.finish_execution(
            plan_id,
            receipt=receipt,
            status=status,
            error_code=result.error_code,
            now=max(self._now(now_override), result.finished_at),
        )
        return self._record_payload(finished, include_plan=False, idempotent=False)

    def _status(self, request: WireRequest) -> dict[str, Any]:
        self._require_unapproved(request)
        if set(request.body) != {"plan_id"}:
            raise BrokerRequestError("invalid_request")
        record = self._store.get(
            self._plan_id(request.body["plan_id"]),
            actor_user_id=request.envelope.actor_id,
            actor_own_id=request.envelope.own_id,
        )
        if request.envelope.job_id != record.plan.continuation_work_item_id:
            raise BrokerRequestError("continuation_mismatch")
        return self._record_payload(record, include_plan=False, idempotent=False)

    def _reconcile_after_restart(self, request: WireRequest, *, now_override: int | None) -> dict[str, Any]:
        if set(request.body) != {"plan_id"} or request.envelope.approval_receipt_id is not None:
            raise BrokerRequestError("invalid_request")
        plan_id = self._plan_id(request.body["plan_id"])
        record = self._store.get(
            plan_id,
            actor_user_id=request.envelope.actor_id,
            actor_own_id=request.envelope.own_id,
        )
        if request.envelope.job_id != record.plan.continuation_work_item_id or not hmac.compare_digest(
            request.envelope.plan_digest, record.plan.digest
        ):
            raise BrokerRequestError("reconciliation_binding_mismatch")
        if (
            record.status is not PlanStatus.UNKNOWN
            or record.error_code != "broker_restart_after_effect_claim"
            or record.receipt is not None
            or record.transaction_id is None
            or record.approval_receipt_id is None
        ):
            raise StoreStateError("reconciliation_not_allowed")
        existing = self._store.idempotent_reconciliation(
            plan_id,
            actor_user_id=request.envelope.actor_id,
            actor_own_id=request.envelope.own_id,
            plan_digest=request.envelope.plan_digest,
            idempotency_key=request.envelope.idempotency_key,
        )
        if existing is not None:
            return self._reconciliation_payload(existing, idempotent=True)
        try:
            observation = self._backend.reconcile_exact(record.plan.transaction)
            if not isinstance(observation, AptReconciliationResult) or not observation.is_consistent_with(
                record.plan.transaction
            ):
                raise TypeError("package backend returned invalid reconciliation evidence")
        except Exception:
            observation = AptReconciliationResult(PackagePostconditionState.UNAVAILABLE, ())
        current = max(self._now(now_override), record.updated_at)
        error_code = {
            PackagePostconditionState.DESIRED: None,
            PackagePostconditionState.PRE_STATE: None,
            PackagePostconditionState.MIXED: "package_state_mixed",
            PackagePostconditionState.UNAVAILABLE: "package_state_unavailable",
        }[observation.postcondition_state]
        receipt = PackageReconciliationReceipt(
            schema_version=BROKER_RECONCILIATION_SCHEMA_VERSION,
            protocol_version=PROTOCOL_VERSION,
            broker_id=self.policy.broker_id,
            broker_build_id=self._build_id,
            reconciliation_id=f"aptrecon_{secrets.token_hex(16)}",
            transaction_id=record.transaction_id,
            plan_id=plan_id,
            plan_digest=record.plan.digest,
            transaction_digest=record.plan.transaction.digest,
            approval_receipt_id=record.approval_receipt_id,
            actor_user_id=record.plan.actor_user_id,
            actor_own_id=record.plan.actor_own_id,
            continuation_work_item_id=record.plan.continuation_work_item_id,
            reconciliation_idempotency_key=request.envelope.idempotency_key,
            transaction_outcome=TransactionOutcome.UNKNOWN,
            postcondition_state=observation.postcondition_state,
            postcondition_satisfied=(observation.postcondition_state is PackagePostconditionState.DESIRED),
            safe_to_replan=(observation.postcondition_state is PackagePostconditionState.PRE_STATE),
            observed_at=current,
            installed=observation.installed,
            error_code=error_code,
        )
        receipt = receipt.with_signature(
            self._authenticator.sign_bytes(
                receipt.canonical_bytes_for_signing(), domain=RECONCILIATION_DOMAIN
            )
        )
        saved = self._store.save_reconciliation(
            plan_id,
            actor_user_id=request.envelope.actor_id,
            actor_own_id=request.envelope.own_id,
            plan_digest=request.envelope.plan_digest,
            idempotency_key=request.envelope.idempotency_key,
            receipt=receipt,
            now=current,
        )
        return self._reconciliation_payload(saved, idempotent=False)

    def _cancel(self, request: WireRequest, *, now_override: int | None) -> dict[str, Any]:
        self._require_unapproved(request)
        if set(request.body) != {"plan_id"}:
            raise BrokerRequestError("invalid_request")
        plan_id = self._plan_id(request.body["plan_id"])
        record = self._store.get(
            plan_id,
            actor_user_id=request.envelope.actor_id,
            actor_own_id=request.envelope.own_id,
        )
        if request.envelope.job_id != record.plan.continuation_work_item_id:
            raise BrokerRequestError("continuation_mismatch")
        cancelled = self._store.cancel_before_commit(
            plan_id,
            actor_user_id=request.envelope.actor_id,
            actor_own_id=request.envelope.own_id,
            now=self._now(now_override),
        )
        return self._record_payload(cancelled, include_plan=False, idempotent=False)

    @staticmethod
    def _record_payload(record: StoredPlan, *, include_plan: bool, idempotent: bool) -> dict[str, Any]:
        result: dict[str, Any] = {
            "error_code": record.error_code,
            "expires_at": record.plan.expires_at,
            "idempotent": idempotent,
            "plan_digest": record.plan.digest,
            "plan_id": record.plan.plan_id,
            "receipt": None if record.receipt is None else record.receipt.to_payload(),
            "status": record.status.value,
            "transaction_id": record.transaction_id,
            "execution_started_at": record.execution_started_at,
            "transaction_digest": record.plan.transaction.digest,
            "updated_at": record.updated_at,
        }
        if include_plan:
            result["plan"] = record.plan.to_payload()
        return result

    @staticmethod
    def _reconciliation_payload(record: StoredPlan, *, idempotent: bool) -> dict[str, Any]:
        if record.reconciliation is None:
            raise StoreStateError("reconciliation_evidence_missing")
        return {
            "error_code": record.error_code,
            "idempotent": idempotent,
            "plan_digest": record.plan.digest,
            "plan_id": record.plan.plan_id,
            "reconciliation": record.reconciliation.to_payload(),
            "status": record.status.value,
            "transaction_digest": record.plan.transaction.digest,
            "transaction_id": record.transaction_id,
            "updated_at": record.updated_at,
        }

    @staticmethod
    def _require_unapproved(request: WireRequest) -> None:
        if (
            request.envelope.approval_receipt_id is not None
            or request.envelope.plan_digest != EMPTY_PLAN_DIGEST
        ):
            raise BrokerRequestError("invalid_request_binding")

    @staticmethod
    def _plan_id(value: object) -> str:
        if not isinstance(value, str) or _PLAN_ID.fullmatch(value) is None:
            raise BrokerRequestError("invalid_plan_id")
        return value

    def _now(self, override: int | None) -> int:
        if override is not None:
            if isinstance(override, bool) or not isinstance(override, int):
                raise BrokerRequestError("invalid_server_time")
            return override
        return int(self._clock())

    def _response(self, request_id: str, *, ok: bool, result: dict[str, Any]) -> bytes:
        response = BrokerWireResponse.create(
            broker_id=self.policy.broker_id,
            build_id=self._build_id,
            request_id=request_id,
            server_time=int(self._clock()),
            ok=ok,
            result=result,
        )
        signature = self._authenticator.sign_bytes(response.signing_bytes(), domain=RESPONSE_DOMAIN)
        return response.with_signature(signature).encode()

    async def serve(
        self,
        socket_path: str | Path,
        *,
        systemd_socket: bool = False,
        client_timeout_sec: float = 10.0,
        shutdown_requested: asyncio.Event | None = None,
    ) -> None:
        if os.geteuid() != 0:
            raise RuntimeError("friday-package-broker must run as root")
        selected = Path(socket_path)
        if not selected.is_absolute() or "\x00" in str(selected):
            raise ValueError("broker socket path must be absolute")
        self._client_timeout_sec = max(1.0, min(float(client_timeout_sec), 60.0))
        owned_identity: tuple[int, int] | None = None
        if systemd_socket:
            inherited = _systemd_socket(selected)
            self._server = await asyncio.start_unix_server(
                self._serve_client,
                sock=inherited,
                limit=MAX_WIRE_BYTES + 1,
                start_serving=True,
            )
        else:
            _prepare_socket_path(selected)
            previous_umask = os.umask(0o077)
            try:
                self._server = await asyncio.start_unix_server(
                    self._serve_client,
                    path=str(selected),
                    limit=MAX_WIRE_BYTES + 1,
                    start_serving=True,
                )
            finally:
                os.umask(previous_umask)
            parent_gid = selected.parent.stat().st_gid
            os.chown(selected, 0, parent_gid)
            os.chmod(selected, 0o660)
            observed = selected.stat(follow_symlinks=False)
            owned_identity = (observed.st_dev, observed.st_ino)
        assert self._server is not None
        self._accepting_requests = True
        try:
            async with self._server:
                if shutdown_requested is None:
                    await self._server.serve_forever()
                else:
                    await shutdown_requested.wait()
                    # Stop admission first, then close the listener and drain every
                    # already-accepted handler.  In particular, do not close SQLite
                    # while an APT commit is still finishing in its worker thread.
                    self._accepting_requests = False
                    self._server.close()
                    await self._server.wait_closed()
                    await self._clients_drained.wait()
        finally:
            self._accepting_requests = False
            if owned_identity is not None:
                _unlink_owned_socket(selected, owned_identity)

    async def _serve_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        current_task = asyncio.current_task()
        if current_task is not None:
            self._active_clients.add(current_task)
            self._clients_drained.clear()
        try:
            peer_uid = _peer_uid(writer)
            try:
                raw = await asyncio.wait_for(reader.readline(), timeout=self._client_timeout_sec)
            except (TimeoutError, asyncio.LimitOverrunError, ValueError):
                raw = b""
            if not self._accepting_requests:
                response = self._response(
                    _canonical_request_id(raw),
                    ok=False,
                    result={"error_code": "broker_draining"},
                )
            elif not raw or len(raw) > MAX_WIRE_BYTES + 1 or not raw.endswith(b"\n"):
                response = self._response("unknown", ok=False, result={"error_code": "invalid_framing"})
            else:
                async with self._semaphore:
                    if not self._accepting_requests:
                        response = self._response(
                            _canonical_request_id(raw),
                            ok=False,
                            result={"error_code": "broker_draining"},
                        )
                    else:
                        response = await asyncio.to_thread(self.handle_request, raw[:-1], peer_uid=peer_uid)
            writer.write(response + b"\n")
            await writer.drain()
        except (ConnectionError, BrokenPipeError):
            pass
        finally:
            writer.close()
            with suppress(ConnectionError):
                await writer.wait_closed()
            if current_task is not None:
                self._active_clients.discard(current_task)
                if not self._active_clients:
                    self._clients_drained.set()


def _terminal_status(outcome: TransactionOutcome) -> PlanStatus:
    if outcome in {TransactionOutcome.COMPLETED, TransactionOutcome.ALREADY_SATISFIED}:
        return PlanStatus.COMPLETED
    if outcome is TransactionOutcome.FAILED_BEFORE_EFFECT:
        return PlanStatus.FAILED_BEFORE_EFFECT
    return PlanStatus.UNKNOWN


def _canonical_request_id(frame: bytes) -> str:
    if not frame or len(frame) > MAX_WIRE_BYTES + 1 or not frame.endswith(b"\n"):
        return "unknown"
    try:
        request = WireRequest.decode(frame[:-1])
    except (ContractError, KeyError, TypeError, ValueError):
        return "unknown"
    if request.encode() != frame[:-1]:
        return "unknown"
    return request.envelope.request_id


def _peer_uid(writer: asyncio.StreamWriter) -> int:
    transport_socket = writer.get_extra_info("socket")
    if transport_socket is None or not hasattr(socket, "SO_PEERCRED"):
        raise BrokerAuthenticationError("peer_unknown")
    credentials = transport_socket.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
    _pid, uid, _gid = struct.unpack("3i", credentials)
    return uid


def _systemd_socket(expected_path: Path) -> socket.socket:
    if os.environ.get("LISTEN_PID") != str(os.getpid()) or os.environ.get("LISTEN_FDS") != "1":
        raise RuntimeError("exactly one systemd socket is required")
    descriptor = 3
    inherited = socket.socket(fileno=os.dup(descriptor))
    if (
        inherited.family != socket.AF_UNIX
        or inherited.getsockopt(socket.SOL_SOCKET, socket.SO_TYPE) != socket.SOCK_STREAM
    ):
        inherited.close()
        raise RuntimeError("systemd descriptor is not a Unix stream socket")
    bound = inherited.getsockname()
    if not isinstance(bound, str) or Path(bound) != expected_path:
        inherited.close()
        raise RuntimeError("systemd socket path does not match broker configuration")
    inherited.setblocking(False)
    return inherited


def _prepare_socket_path(path: Path) -> None:
    path.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
    parent = path.parent.stat(follow_symlinks=False)
    if (
        not stat.S_ISDIR(parent.st_mode)
        or parent.st_uid != 0
        or parent.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
    ):
        raise RuntimeError("broker socket directory is unsafe")
    try:
        observed = path.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISSOCK(observed.st_mode) or observed.st_uid != 0:
        raise RuntimeError("broker socket path already exists")
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        probe.settimeout(0.2)
        probe.connect(str(path))
    except (ConnectionRefusedError, FileNotFoundError):
        path.unlink()
    else:
        raise RuntimeError("another broker already owns the socket")
    finally:
        probe.close()


def _unlink_owned_socket(path: Path, identity: tuple[int, int]) -> None:
    try:
        observed = path.stat(follow_symlinks=False)
    except FileNotFoundError:
        return
    if stat.S_ISSOCK(observed.st_mode) and (observed.st_dev, observed.st_ino) == identity:
        path.unlink()


__all__ = ["EMPTY_PLAN_DIGEST", "BrokerRequestError", "PackageBrokerDaemon"]
