"""Caller-side broker client with pinned Ed25519 evidence verification."""

from __future__ import annotations

import os
import re
import secrets
import socket
import stat
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from friday.host_control.contracts import MAX_WIRE_BYTES, WireRequest

from .approval import PackageApprovalProof
from .authentication import (
    RECEIPT_DOMAIN,
    RECONCILIATION_DOMAIN,
    RESPONSE_DOMAIN,
    BrokerAuthenticator,
)
from .contracts import (
    BROKER_RECEIPT_SCHEMA_VERSION,
    EMPTY_PLAN_DIGEST,
    AptInstallPlan,
    BrokerContractError,
    BrokerWireResponse,
    PackageReconciliationReceipt,
    PackageRef,
    PackageTransactionReceipt,
    TransactionOutcome,
)

_PLAN_ID = re.compile(r"^aptplan_[0-9a-f]{16,64}$")
_ERROR_CODE = re.compile(r"^[a-z][a-z0-9_]{0,79}$")
_SAFE_EXECUTE_REJECTIONS = frozenset(
    {
        "approval_required",
        "approval_binding_mismatch",
        "approval_expired",
        "approval_from_future",
        "approval_proof_required",
        "approval_signature_invalid",
        "replayed_approval",
        "approved_plan_mismatch",
        "broker_draining",
        "continuation_mismatch",
        "execution_idempotency_conflict",
        "idempotency_conflict",
        "invalid_plan_id",
        "invalid_request",
        "invalid_request_binding",
        "plan_drift",
        "plan_not_executable",
        "plan_not_found",
        "request_expired",
    }
)
_RECORD_FIELDS = {
    "error_code",
    "execution_started_at",
    "expires_at",
    "idempotent",
    "plan_digest",
    "plan_id",
    "receipt",
    "status",
    "transaction_digest",
    "transaction_id",
    "updated_at",
}


class PackageBrokerClientError(RuntimeError):
    code = "package_broker_client_error"


class PackageBrokerUnavailable(PackageBrokerClientError):
    code = "package_broker_unavailable"


class PackageBrokerRejected(PackageBrokerClientError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class PackageBrokerUnknownOutcome(PackageBrokerClientError):
    code = "package_broker_outcome_unknown"

    def __init__(self, *, request_id: str, idempotency_key: str, plan_id: str) -> None:
        super().__init__(self.code)
        self.request_id = request_id
        self.idempotency_key = idempotency_key
        self.plan_id = plan_id


class PackageBrokerClient:
    """Only the reviewed closed broker methods are exposed as public operations."""

    def __init__(
        self,
        *,
        socket_path: str | Path,
        broker_id: str,
        request_key: bytes,
        pinned_public_key: bytes,
        timeout_sec: float = 300.0,
        request_ttl_sec: int = 120,
        clock: Callable[[], int] | None = None,
    ) -> None:
        selected = Path(socket_path)
        if not selected.is_absolute() or "\x00" in str(selected):
            raise ValueError("package-broker socket path must be absolute")
        if not 1.0 <= timeout_sec <= 300.0 or not 1 <= request_ttl_sec <= 300:
            raise ValueError("package-broker client timing is invalid")
        self.socket_path = selected
        self.broker_id = broker_id
        self._authenticator = BrokerAuthenticator(
            request_key,
            broker_id=broker_id,
            verification_public_key=pinned_public_key,
        )
        self._timeout_sec = float(timeout_sec)
        self._request_ttl_sec = int(request_ttl_sec)
        self._clock = clock or (lambda: int(time.time()))
        self._sequence = secrets.randbits(61)
        self._sequence_lock = threading.Lock()

    def health(self, *, actor_id: str, own_id: str, job_id: str, idempotency_key: str) -> dict[str, Any]:
        result = self._request(
            method="Health",
            body={},
            actor_id=actor_id,
            own_id=own_id,
            job_id=job_id,
            idempotency_key=idempotency_key,
            plan_digest=EMPTY_PLAN_DIGEST,
            approval_receipt_id=None,
            effectful=False,
        )
        expected = {
            "broker_id",
            "build_id",
            "methods",
            "package_backend",
            "protocol_versions",
        }
        backend = result.get("package_backend")
        if (
            set(result) != expected
            or result["broker_id"] != self.broker_id
            or not _valid_utf8_text(result["build_id"], maximum=160)
            or result["methods"]
            != [
                "CancelBeforeCommit",
                "ExecuteInstall",
                "Health",
                "PlanInstall",
                "ReconcileAfterRestart",
                "Status",
            ]
            or result["protocol_versions"] != ["1.0"]
            or not _valid_backend_health(backend)
        ):
            raise PackageBrokerUnavailable(self.broker_id)
        return result

    def plan_install(
        self,
        *,
        requested: tuple[PackageRef, ...],
        original_task_ref: str,
        continuation_work_item_id: str,
        actor_id: str,
        own_id: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        if (
            not requested
            or len(requested) > 16
            or any(not isinstance(item, PackageRef) for item in requested)
        ):
            raise ValueError("package install request is invalid")
        result = self._request(
            method="PlanInstall",
            body={
                "continuation_work_item_id": continuation_work_item_id,
                "original_task_ref": original_task_ref,
                "requested": [item.to_payload() for item in requested],
            },
            actor_id=actor_id,
            own_id=own_id,
            job_id=continuation_work_item_id,
            idempotency_key=idempotency_key,
            plan_digest=EMPTY_PLAN_DIGEST,
            approval_receipt_id=None,
            effectful=False,
        )
        try:
            plan = self._validate_record(result, include_plan=True)
            assert plan is not None
            self._validate_plan_binding(
                plan,
                requested=requested,
                original_task_ref=original_task_ref,
                continuation_work_item_id=continuation_work_item_id,
                actor_id=actor_id,
                own_id=own_id,
            )
        except BrokerContractError as exc:
            raise PackageBrokerUnavailable(self.broker_id) from exc
        return result

    def execute_install(
        self,
        *,
        plan_id: str,
        approved_plan_digest: str,
        approval_receipt_id: str,
        approval_proof: PackageApprovalProof,
        continuation_work_item_id: str,
        actor_id: str,
        own_id: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        self._validate_plan_id(plan_id)
        if not isinstance(approval_proof, PackageApprovalProof):
            raise ValueError("package approval proof is required")
        if (
            approval_proof.broker_id != self.broker_id
            or approval_proof.plan_id != plan_id
            or approval_proof.plan_digest != approved_plan_digest
            or approval_proof.approval_receipt_id != approval_receipt_id
            or approval_proof.continuation_work_item_id != continuation_work_item_id
            or approval_proof.actor_user_id != actor_id
            or approval_proof.actor_own_id != own_id
            or approval_proof.execution_idempotency_key != idempotency_key
        ):
            raise ValueError("package approval proof binding is invalid")
        result = self._request(
            method="ExecuteInstall",
            body={"approval_proof": approval_proof.to_payload(), "plan_id": plan_id},
            actor_id=actor_id,
            own_id=own_id,
            job_id=continuation_work_item_id,
            idempotency_key=idempotency_key,
            plan_digest=approved_plan_digest,
            approval_receipt_id=approval_receipt_id,
            effectful=True,
            effect_plan_id=plan_id,
        )
        try:
            self._validate_record(
                result,
                include_plan=False,
                expected_plan_digest=approved_plan_digest,
                expected_approval_id=approval_receipt_id,
                expected_idempotency_key=idempotency_key,
                expected_plan_id=plan_id,
                allowed_statuses={"executing", "completed", "failed_before_effect", "unknown"},
                require_current_receipt=True,
            )
        except BrokerContractError as exc:
            raise PackageBrokerUnknownOutcome(
                request_id="unavailable",
                idempotency_key=idempotency_key,
                plan_id=plan_id,
            ) from exc
        return result

    def reconcile_after_restart(
        self,
        *,
        plan_id: str,
        plan_digest: str,
        continuation_work_item_id: str,
        actor_id: str,
        own_id: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        self._validate_plan_id(plan_id)
        if not _is_digest(plan_digest):
            raise ValueError("package reconciliation plan digest is invalid")
        result = self._request(
            method="ReconcileAfterRestart",
            body={"plan_id": plan_id},
            actor_id=actor_id,
            own_id=own_id,
            job_id=continuation_work_item_id,
            idempotency_key=idempotency_key,
            plan_digest=plan_digest,
            approval_receipt_id=None,
            effectful=False,
        )
        expected = {
            "error_code",
            "idempotent",
            "plan_digest",
            "plan_id",
            "reconciliation",
            "status",
            "transaction_digest",
            "transaction_id",
            "updated_at",
        }
        try:
            if (
                set(result) != expected
                or result["status"] != "unknown"
                or result["error_code"] != "broker_restart_after_effect_claim"
                or result["plan_id"] != plan_id
                or result["plan_digest"] != plan_digest
                or not _is_digest(result["transaction_digest"])
                or not isinstance(result["idempotent"], bool)
                or isinstance(result["updated_at"], bool)
                or not isinstance(result["updated_at"], int)
                or not isinstance(result["transaction_id"], str)
                or re.fullmatch(r"^apttxn_[0-9a-f]{16,64}$", result["transaction_id"]) is None
            ):
                raise BrokerContractError("broker reconciliation response is invalid")
            receipt = PackageReconciliationReceipt.from_payload(result["reconciliation"])
            if (
                receipt.broker_id != self.broker_id
                or receipt.plan_id != plan_id
                or receipt.plan_digest != plan_digest
                or receipt.transaction_digest != result["transaction_digest"]
                or receipt.transaction_id != result["transaction_id"]
                or receipt.actor_user_id != actor_id
                or receipt.actor_own_id != own_id
                or receipt.continuation_work_item_id != continuation_work_item_id
                or receipt.reconciliation_idempotency_key != idempotency_key
                or receipt.observed_at > result["updated_at"]
                or not self._authenticator.verify_bytes(
                    receipt.canonical_bytes_for_signing(),
                    receipt.signature,
                    domain=RECONCILIATION_DOMAIN,
                )
            ):
                raise BrokerContractError("broker reconciliation binding is invalid")
        except (KeyError, TypeError, ValueError, BrokerContractError) as exc:
            raise PackageBrokerUnavailable(self.broker_id) from exc
        return result

    def status(
        self,
        *,
        plan_id: str,
        continuation_work_item_id: str,
        actor_id: str,
        own_id: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        self._validate_plan_id(plan_id)
        result = self._request(
            method="Status",
            body={"plan_id": plan_id},
            actor_id=actor_id,
            own_id=own_id,
            job_id=continuation_work_item_id,
            idempotency_key=idempotency_key,
            plan_digest=EMPTY_PLAN_DIGEST,
            approval_receipt_id=None,
            effectful=False,
        )
        try:
            self._validate_record(result, include_plan=False, expected_plan_id=plan_id)
        except BrokerContractError as exc:
            raise PackageBrokerUnavailable(self.broker_id) from exc
        return result

    def cancel_before_commit(
        self,
        *,
        plan_id: str,
        continuation_work_item_id: str,
        actor_id: str,
        own_id: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        self._validate_plan_id(plan_id)
        result = self._request(
            method="CancelBeforeCommit",
            body={"plan_id": plan_id},
            actor_id=actor_id,
            own_id=own_id,
            job_id=continuation_work_item_id,
            idempotency_key=idempotency_key,
            plan_digest=EMPTY_PLAN_DIGEST,
            approval_receipt_id=None,
            effectful=True,
            effect_plan_id=plan_id,
        )
        try:
            self._validate_record(
                result,
                include_plan=False,
                expected_plan_id=plan_id,
                allowed_statuses={"cancelled_before_commit"},
            )
        except BrokerContractError as exc:
            raise PackageBrokerUnknownOutcome(
                request_id="unavailable",
                idempotency_key=idempotency_key,
                plan_id=plan_id,
            ) from exc
        return result

    def _request(
        self,
        *,
        method: str,
        body: dict[str, Any],
        actor_id: str,
        own_id: str,
        job_id: str,
        idempotency_key: str,
        plan_digest: str,
        approval_receipt_id: str | None,
        effectful: bool,
        effect_plan_id: str = "",
    ) -> dict[str, Any]:
        issued_at = int(self._clock())
        request_id = f"brokerreq_{secrets.token_hex(16)}"
        envelope = self._authenticator.create_envelope(
            request_id=request_id,
            sequence=self._next_sequence(),
            issued_at=issued_at,
            expires_at=issued_at + self._request_ttl_sec,
            method=method,
            job_id=job_id,
            actor_id=actor_id,
            own_id=own_id,
            idempotency_key=idempotency_key,
            plan_digest=plan_digest,
            body=body,
            approval_receipt_id=approval_receipt_id,
        )
        raw = WireRequest.create(envelope, body).encode()
        connected = False
        operation_deadline = time.monotonic() + self._timeout_sec
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as channel:
                channel.settimeout(_remaining_timeout(operation_deadline))
                channel.connect(str(self.socket_path))
                connected = True
                channel.settimeout(_remaining_timeout(operation_deadline))
                channel.sendall(raw + b"\n")
                response_raw = _read_response(channel, deadline=operation_deadline)
            response = BrokerWireResponse.decode(response_raw)
            if response.encode() != response_raw:
                raise BrokerContractError("broker response is not canonical")
            if response.broker_id != self.broker_id or response.request_id != request_id:
                raise BrokerContractError("broker response identity mismatch")
            if not self._authenticator.verify_bytes(
                response.signing_bytes(), response.signature, domain=RESPONSE_DOMAIN
            ):
                raise BrokerContractError("broker response signature is invalid")
            if not response.ok:
                error_code = response.result["error_code"]
                if effectful and error_code not in _SAFE_EXECUTE_REJECTIONS:
                    raise PackageBrokerUnknownOutcome(
                        request_id=request_id,
                        idempotency_key=idempotency_key,
                        plan_id=effect_plan_id,
                    )
                raise PackageBrokerRejected(error_code)
            return response.result
        except PackageBrokerRejected:
            raise
        except PackageBrokerUnknownOutcome:
            raise
        except (OSError, TimeoutError, BrokerContractError, ValueError) as exc:
            if effectful and connected:
                raise PackageBrokerUnknownOutcome(
                    request_id=request_id,
                    idempotency_key=idempotency_key,
                    plan_id=effect_plan_id,
                ) from exc
            raise PackageBrokerUnavailable(self.broker_id) from exc

    def _validate_record(
        self,
        result: dict[str, Any],
        *,
        include_plan: bool,
        expected_plan_digest: str | None = None,
        expected_approval_id: str | None = None,
        expected_idempotency_key: str | None = None,
        expected_plan_id: str | None = None,
        allowed_statuses: set[str] | None = None,
        require_current_receipt: bool = False,
    ) -> AptInstallPlan | None:
        expected = _RECORD_FIELDS | ({"plan"} if include_plan else set())
        if set(result) != expected:
            raise BrokerContractError("broker record response fields are invalid")
        plan_id = self._validate_plan_id(result["plan_id"])
        status = result["status"]
        error_code = result["error_code"]
        if (
            not isinstance(result["idempotent"], bool)
            or not isinstance(status, str)
            or status
            not in {
                "planned",
                "executing",
                "completed",
                "cancelled_before_commit",
                "failed_before_effect",
                "unknown",
            }
            or any(
                isinstance(result[field], bool) or not isinstance(result[field], int)
                for field in ("expires_at", "updated_at")
            )
            or not _is_digest(result["plan_digest"])
            or not _is_digest(result["transaction_digest"])
            or (
                error_code is not None
                and (not isinstance(error_code, str) or _ERROR_CODE.fullmatch(error_code) is None)
            )
        ):
            raise BrokerContractError("broker record response values are invalid")
        if expected_plan_id is not None and plan_id != expected_plan_id:
            raise BrokerContractError("broker record response plan identity mismatch")
        if allowed_statuses is not None and status not in allowed_statuses:
            raise BrokerContractError("broker record response status is invalid for the method")
        transaction_id = result["transaction_id"]
        execution_started_at = result["execution_started_at"]
        if (transaction_id is None) != (execution_started_at is None):
            raise BrokerContractError("broker execution attempt metadata is incomplete")
        if transaction_id is not None and (
            not isinstance(transaction_id, str)
            or re.fullmatch(r"^apttxn_[0-9a-f]{16,64}$", transaction_id) is None
            or isinstance(execution_started_at, bool)
            or not isinstance(execution_started_at, int)
        ):
            raise BrokerContractError("broker execution attempt metadata is invalid")
        receipt_payload = result["receipt"]
        if status in {"planned", "cancelled_before_commit"} and (
            transaction_id is not None or receipt_payload is not None or error_code is not None
        ):
            raise BrokerContractError("broker unexecuted record contains execution evidence")
        if status == "executing" and (
            transaction_id is None or receipt_payload is not None or error_code is not None
        ):
            raise BrokerContractError("broker executing record is inconsistent")
        if status == "completed" and (
            transaction_id is None or receipt_payload is None or error_code is not None
        ):
            raise BrokerContractError("broker completed record is inconsistent")
        if status == "failed_before_effect" and error_code is None:
            raise BrokerContractError("broker failed record lacks an error code")
        if status == "unknown" and (transaction_id is None or error_code is None):
            raise BrokerContractError("broker unknown record lacks durable execution evidence")
        plan: AptInstallPlan | None = None
        if include_plan:
            plan = AptInstallPlan.from_payload(result["plan"])
            if plan.plan_id != plan_id or plan.digest != result["plan_digest"]:
                raise BrokerContractError("broker plan response digest mismatch")
            if plan.transaction.digest != result["transaction_digest"]:
                raise BrokerContractError("broker transaction response digest mismatch")
            if plan.expires_at != result["expires_at"]:
                raise BrokerContractError("broker plan response expiry mismatch")
        if expected_plan_digest is not None and result["plan_digest"] != expected_plan_digest:
            raise BrokerContractError("broker execution response plan mismatch")
        if receipt_payload is not None:
            receipt = PackageTransactionReceipt.from_payload(receipt_payload)
            if require_current_receipt and receipt.schema_version != BROKER_RECEIPT_SCHEMA_VERSION:
                raise BrokerContractError("new package effect uses a legacy receipt")
            if (
                receipt.broker_id != self.broker_id
                or receipt.plan_id != plan_id
                or receipt.approved_plan_digest != result["plan_digest"]
            ):
                raise BrokerContractError("broker receipt binding mismatch")
            if transaction_id != receipt.transaction_id:
                raise BrokerContractError("broker receipt transaction identity mismatch")
            if receipt.error_code != error_code:
                raise BrokerContractError("broker receipt error mismatch")
            expected_outcomes = {
                "completed": {TransactionOutcome.COMPLETED, TransactionOutcome.ALREADY_SATISFIED},
                "failed_before_effect": {TransactionOutcome.FAILED_BEFORE_EFFECT},
                "unknown": {TransactionOutcome.UNKNOWN},
            }
            if receipt.outcome not in expected_outcomes.get(status, set()):
                raise BrokerContractError("broker receipt outcome mismatch")
            if receipt.effect_boundary_crossed and (
                receipt.executed_transaction_digest != result["transaction_digest"]
            ):
                raise BrokerContractError("broker effect receipt transaction mismatch")
            assert execution_started_at is not None
            if receipt.started_at < execution_started_at or receipt.finished_at > result["updated_at"]:
                raise BrokerContractError("broker receipt timing is outside its execution record")
            if expected_approval_id is not None and receipt.approval_receipt_id != expected_approval_id:
                raise BrokerContractError("broker receipt approval mismatch")
            if expected_idempotency_key is not None and receipt.idempotency_key != expected_idempotency_key:
                raise BrokerContractError("broker receipt idempotency mismatch")
            if not self._authenticator.verify_bytes(
                receipt.canonical_bytes_for_signing(),
                receipt.signature,
                domain=RECEIPT_DOMAIN,
            ):
                raise BrokerContractError("broker receipt signature is invalid")
        return plan

    def _validate_plan_binding(
        self,
        plan: AptInstallPlan,
        *,
        requested: tuple[PackageRef, ...],
        original_task_ref: str,
        continuation_work_item_id: str,
        actor_id: str,
        own_id: str,
    ) -> None:
        if (
            plan.broker_id != self.broker_id
            or plan.actor_user_id != actor_id
            or plan.actor_own_id != own_id
            or plan.original_task_ref != original_task_ref
            or plan.continuation_work_item_id != continuation_work_item_id
        ):
            raise BrokerContractError("broker plan identity binding mismatch")
        resolved = {item.name: item for item in plan.transaction.requested}
        if len(resolved) != len(requested) or set(resolved) != {item.name for item in requested}:
            raise BrokerContractError("broker plan requested package set mismatch")
        for item in requested:
            selected = resolved[item.name]
            if (item.version is not None and selected.version != item.version) or (
                item.architecture is not None and selected.architecture != item.architecture
            ):
                raise BrokerContractError("broker plan package constraint mismatch")

    def _next_sequence(self) -> int:
        with self._sequence_lock:
            self._sequence += 1
            return self._sequence

    @staticmethod
    def _validate_plan_id(value: object) -> str:
        if not isinstance(value, str) or _PLAN_ID.fullmatch(value) is None:
            raise BrokerContractError("broker plan id is invalid")
        return value


def _read_response(channel: socket.socket, *, deadline: float) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        channel.settimeout(_remaining_timeout(deadline))
        block = channel.recv(min(64 * 1024, MAX_WIRE_BYTES + 2 - total))
        if not block:
            raise ConnectionError("broker response ended before framing")
        chunks.append(block)
        total += len(block)
        if total > MAX_WIRE_BYTES + 1:
            raise BrokerContractError("broker response exceeds wire limit")
        joined = b"".join(chunks)
        if joined.endswith(b"\n"):
            return joined[:-1]
        if b"\n" in joined:
            raise BrokerContractError("broker response contains trailing frames")


def _remaining_timeout(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("package-broker operation deadline expired")
    return remaining


def _is_digest(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"^[0-9a-f]{64}$", value) is not None


def _valid_backend_health(value: object) -> bool:
    expected = {
        "available",
        "broken_package_count",
        "dpkg_journal_dirty",
        "error_code",
        "manager",
        "manager_version",
    }
    if not isinstance(value, dict) or set(value) != expected:
        return False
    available = value["available"]
    broken = value["broken_package_count"]
    dirty = value["dpkg_journal_dirty"]
    error_code = value["error_code"]
    manager_version = value["manager_version"]
    if (
        not isinstance(available, bool)
        or not isinstance(dirty, bool)
        or isinstance(broken, bool)
        or not isinstance(broken, int)
        or not 0 <= broken <= 1_000_000
        or value["manager"] != "apt"
        or not _valid_utf8_text(manager_version, maximum=160)
        or (
            error_code is not None
            and (not isinstance(error_code, str) or _ERROR_CODE.fullmatch(error_code) is None)
        )
    ):
        return False
    if available:
        return error_code is None and not dirty and broken == 0
    return error_code is not None


def _valid_utf8_text(value: object, *, maximum: int) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        return len(value.encode("utf-8", errors="strict")) <= maximum
    except UnicodeEncodeError:
        return False


def load_pinned_public_key(path: str | Path) -> bytes:
    """Load one root-owned, non-replaceable raw Ed25519 public key."""

    selected = Path(path)
    parent = selected.parent
    if parent.resolve(strict=False) != parent:
        raise BrokerContractError("broker public key path cannot traverse symlinks")
    parent_metadata = parent.stat(follow_symlinks=False)
    if (
        not stat.S_ISDIR(parent_metadata.st_mode)
        or parent_metadata.st_uid != 0
        or parent_metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
    ):
        raise BrokerContractError("broker public key parent directory is unsafe")
    descriptor = -1
    try:
        descriptor = os.open(selected, os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0))
        observed = os.fstat(descriptor)
        if (
            not stat.S_ISREG(observed.st_mode)
            or observed.st_nlink != 1
            or observed.st_uid != 0
            or observed.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
            or observed.st_size != 32
        ):
            raise BrokerContractError("broker public key metadata is unsafe")
        payload = os.read(descriptor, 33)
    except OSError as exc:
        raise BrokerContractError("broker public key could not be read safely") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if len(payload) != 32:
        raise BrokerContractError("broker public key material is invalid")
    return payload


__all__ = [
    "PackageBrokerClient",
    "PackageBrokerClientError",
    "PackageBrokerRejected",
    "PackageBrokerUnavailable",
    "PackageBrokerUnknownOutcome",
    "load_pinned_public_key",
]
