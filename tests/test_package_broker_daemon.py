from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from friday.host_control.contracts import WireRequest, canonical_digest, canonical_json_bytes
from friday_package_broker import daemon as daemon_module
from friday_package_broker.approval import (
    PackageApprovalProof,
    PackageApprovalSigner,
    PackageApprovalVerifier,
)
from friday_package_broker.apt_backend import (
    AptBackendHealth,
    AptExecutionResult,
    AptReconciliationResult,
)
from friday_package_broker.authentication import (
    RECEIPT_DOMAIN,
    RECONCILIATION_DOMAIN,
    RESPONSE_DOMAIN,
    BrokerAuthenticator,
    ReplayLedger,
)
from friday_package_broker.contracts import (
    AptTransaction,
    InstalledPackage,
    PackageEvidenceReference,
    PackagePostconditionState,
    PackageReconciliationReceipt,
    PackageRef,
    TransactionOutcome,
)
from friday_package_broker.daemon import EMPTY_PLAN_DIGEST, PackageBrokerDaemon
from friday_package_broker.evidence import OutputCapture, PackageEvidenceStore
from friday_package_broker.policy import BrokerPolicy
from friday_package_broker.store import BrokerStore
from tests.package_broker_fixtures import transaction


class FakeBackend:
    def __init__(self) -> None:
        self.execute_calls = 0
        self.plan_calls = 0
        self.drift = False
        self.untrusted = False
        self.installed = False
        self.reconcile_calls = 0
        self.reconciliation_state = PackagePostconditionState.DESIRED

    def health(self) -> AptBackendHealth:
        return AptBackendHealth(True, "apt", "2.8.0")

    def plan(self, requested: tuple[PackageRef, ...]) -> AptTransaction:
        self.plan_calls += 1
        assert tuple(item.name for item in requested) == ("nmap",)
        planned = transaction(site="mirror.example" if self.drift else "archive.ubuntu.com")
        if self.untrusted:
            origin = replace(planned.changes[0].origins[0], trusted=False)
            planned = replace(planned, changes=(replace(planned.changes[0], origins=(origin,)),))
        return planned

    def execute_exact(self, planned: AptTransaction, *, deadline: int | None = None) -> AptExecutionResult:
        self.execute_calls += 1
        assert deadline is not None
        assert planned.digest == transaction().digest
        empty_digest = hashlib.sha256(b"").hexdigest()
        evidence_digest = "e" * 64
        self.installed = True
        return AptExecutionResult(
            outcome=TransactionOutcome.COMPLETED,
            effect_boundary_crossed=True,
            started_at=1_001,
            finished_at=1_002,
            exit_code=0,
            lock_state="released",
            before=(),
            after=(InstalledPackage("nmap", "7.94", "amd64"),),
            output_capture_status="captured",
            stdout_sha256=empty_digest,
            stdout_size_bytes=0,
            stderr_sha256=empty_digest,
            stderr_size_bytes=0,
            stdout_total_size_bytes=0,
            stderr_total_size_bytes=0,
            stdout_total_size_complete=True,
            stderr_total_size_complete=True,
            evidence_refs=(
                PackageEvidenceReference(
                    kind="apt_stdout",
                    ref=f"evidence/{empty_digest}.stdout",
                    sha256=empty_digest,
                    size_bytes=0,
                    media_type="application/octet-stream",
                ),
                PackageEvidenceReference(
                    kind="apt_stderr",
                    ref=f"evidence/{empty_digest}.stderr",
                    sha256=empty_digest,
                    size_bytes=0,
                    media_type="application/octet-stream",
                ),
                PackageEvidenceReference(
                    kind="apt_dpkg_transaction",
                    ref=f"evidence/{evidence_digest}.json",
                    sha256=evidence_digest,
                    size_bytes=512,
                ),
            ),
            service_unit_observation_status="captured",
            manager_version="2.8.0",
        )

    def reconcile_exact(self, planned: AptTransaction) -> AptReconciliationResult:
        self.reconcile_calls += 1
        assert planned.digest == transaction().digest
        installed = {
            PackagePostconditionState.DESIRED: (InstalledPackage("nmap", "7.94", "amd64"),),
            PackagePostconditionState.PRE_STATE: (),
            PackagePostconditionState.MIXED: (InstalledPackage("nmap", "7.93", "amd64"),),
            PackagePostconditionState.UNAVAILABLE: (),
        }[self.reconciliation_state]
        return AptReconciliationResult(self.reconciliation_state, installed)


class RawEvidenceBackend(FakeBackend):
    def __init__(self, evidence_store: PackageEvidenceStore, raw_stdout: bytes) -> None:
        super().__init__()
        self.evidence_store = evidence_store
        self.raw_stdout = raw_stdout

    def execute_exact(self, planned: AptTransaction, *, deadline: int | None = None) -> AptExecutionResult:
        base = super().execute_exact(planned, deadline=deadline)
        capture = OutputCapture(
            status="captured",
            stdout_bytes=self.raw_stdout,
            stderr_bytes=b"",
            stdout_total_size_bytes=len(self.raw_stdout),
            stderr_total_size_bytes=0,
            stdout_total_size_complete=True,
            stderr_total_size_complete=True,
        )
        refs = self.evidence_store.persist_transaction(
            transaction_digest=planned.digest,
            outcome="completed",
            error_code=None,
            output=capture,
            service_unit_observation_status="captured",
            service_unit_observations=(),
        )
        return replace(
            base,
            stdout_sha256=capture.stdout_sha256,
            stdout_size_bytes=capture.stdout_size_bytes,
            stderr_sha256=capture.stderr_sha256,
            stderr_size_bytes=capture.stderr_size_bytes,
            stdout_total_size_bytes=capture.stdout_total_size_bytes,
            stderr_total_size_bytes=capture.stderr_total_size_bytes,
            stdout_total_size_complete=capture.stdout_total_size_complete,
            stderr_total_size_complete=capture.stderr_total_size_complete,
            evidence_refs=refs,
        )


class Harness:
    def __init__(
        self,
        *,
        backend: FakeBackend | None = None,
        store: BrokerStore | None = None,
    ) -> None:
        self.auth = BrokerAuthenticator(b"K" * 32, broker_id="test-broker", signing_private_key=b"S" * 32)
        self.replay = ReplayLedger(":memory:", allow_memory=True)
        self.store = store or BrokerStore(":memory:", allow_memory=True)
        self.backend = backend or FakeBackend()
        self.approval_signer = PackageApprovalSigner(b"A" * 32)
        self.last_approval_proof: PackageApprovalProof | None = None
        self.daemon = PackageBrokerDaemon(
            policy=BrokerPolicy(
                broker_id="test-broker",
                allowed_peer_uids=frozenset({1000}),
                allowed_packages=frozenset({"nmap"}),
            ),
            authenticator=self.auth,
            replay_ledger=self.replay,
            store=self.store,
            backend=self.backend,
            approval_verifier=PackageApprovalVerifier(self.approval_signer.public_key_bytes),
            build_id="test-build",
        )
        self.sequence = 0

    def close(self) -> None:
        self.replay.close()
        self.store.close()

    def approval_payload_digest(
        self,
        plan_id: str,
        *,
        actor_id: str = "owner",
        own_id: str = "own-1",
    ) -> str:
        record = self.store.get(
            plan_id,
            actor_user_id=actor_id,
            actor_own_id=own_id,
        )
        return canonical_digest(
            {
                "job_id": record.plan.continuation_work_item_id,
                "package_plan": record.plan.to_payload(),
                "plan_digest": record.plan.digest,
            }
        )

    def request(
        self,
        method: str,
        body: dict[str, Any],
        *,
        idempotency_key: str,
        plan_digest: str = EMPTY_PLAN_DIGEST,
        approval_receipt_id: str | None = None,
        actor_id: str = "owner",
        own_id: str = "own-1",
        job_id: str = "work-1",
        raw_only: bool = False,
        approval_proof: PackageApprovalProof | None = None,
        auto_approval_proof: bool = True,
        now: int = 1_000,
    ) -> dict[str, Any] | bytes:
        if method == "ExecuteInstall" and approval_receipt_id is not None:
            plan_id = str(body.get("plan_id") or "")
            if approval_proof is None and auto_approval_proof:
                approval_proof = self.approval_signer.issue(
                    broker_id="test-broker",
                    approval_receipt_id=approval_receipt_id,
                    approval_payload_digest=self.approval_payload_digest(
                        plan_id,
                        actor_id=actor_id,
                        own_id=own_id,
                    ),
                    plan_id=plan_id,
                    plan_digest=plan_digest,
                    actor_user_id=actor_id,
                    actor_own_id=own_id,
                    continuation_work_item_id=job_id,
                    execution_idempotency_key=idempotency_key,
                    issued_at=1_000,
                    expires_at=1_100,
                )
            if approval_proof is not None:
                self.last_approval_proof = approval_proof
                body = {**body, "approval_proof": approval_proof.to_payload()}
        self.sequence += 1
        envelope = self.auth.create_envelope(
            request_id=f"request-{self.sequence}",
            sequence=self.sequence,
            issued_at=now,
            expires_at=now + 100,
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
        if raw_only:
            return raw
        response = json.loads(self.daemon.handle_request(raw, peer_uid=1000, now=now))
        signature = response.pop("signature")
        assert self.auth.verify_bytes(canonical_json_bytes(response), signature, domain=RESPONSE_DOMAIN)
        return response

    @staticmethod
    def plan_body(*, task: str = "task-1") -> dict[str, Any]:
        return {
            "continuation_work_item_id": "work-1",
            "original_task_ref": task,
            "requested": [PackageRef("nmap").to_payload()],
        }


def test_closed_api_plans_executes_once_and_returns_a_signed_bounded_receipt() -> None:
    harness = Harness()
    try:
        health = harness.request("Health", {}, idempotency_key="health-1")
        assert isinstance(health, dict) and health["ok"] is True
        assert health["result"]["methods"] == [
            "CancelBeforeCommit",
            "ExecuteInstall",
            "Health",
            "PlanInstall",
            "ReconcileAfterRestart",
            "Status",
        ]

        planned = harness.request("PlanInstall", harness.plan_body(), idempotency_key="plan-1")
        assert isinstance(planned, dict) and planned["ok"] is True
        plan_id = planned["result"]["plan_id"]
        plan_digest = planned["result"]["plan_digest"]
        executed = harness.request(
            "ExecuteInstall",
            {"plan_id": plan_id},
            idempotency_key="execute-1",
            plan_digest=plan_digest,
            approval_receipt_id="approval-1",
        )
        assert isinstance(executed, dict) and executed["result"]["status"] == "completed"
        receipt = executed["result"]["receipt"]
        assert receipt["effect_boundary_crossed"] is True
        assert "stdout" not in receipt and "stderr" not in receipt
        assert receipt["output_capture_status"] == "captured"
        assert receipt["evidence_refs"][0]["ref"].startswith("evidence/")
        assert receipt["service_unit_observation_status"] == "captured"
        receipt_signature = receipt.pop("signature")
        assert harness.auth.verify_bytes(
            canonical_json_bytes(receipt), receipt_signature, domain=RECEIPT_DOMAIN
        )
        assert harness.backend.execute_calls == 1

        repeated = harness.request(
            "ExecuteInstall",
            {"plan_id": plan_id},
            idempotency_key="execute-1",
            plan_digest=plan_digest,
            approval_receipt_id="approval-1",
            approval_proof=harness.last_approval_proof,
        )
        assert isinstance(repeated, dict) and repeated["result"]["idempotent"] is True
        assert harness.backend.execute_calls == 1
    finally:
        harness.close()


def test_raw_evidence_never_enters_receipt_sqlite_event_journal_or_projection(
    tmp_path: Path,
) -> None:
    raw_stdout = b"private-output-mutation-canary"
    evidence_store = PackageEvidenceStore(tmp_path / "evidence")
    harness = Harness(backend=RawEvidenceBackend(evidence_store, raw_stdout))
    try:
        planned = harness.request("PlanInstall", harness.plan_body(), idempotency_key="plan-1")
        assert isinstance(planned, dict)
        executed = harness.request(
            "ExecuteInstall",
            {"plan_id": planned["result"]["plan_id"]},
            idempotency_key="execute-1",
            plan_digest=planned["result"]["plan_digest"],
            approval_receipt_id="approval-1",
        )
        assert isinstance(executed, dict)
        receipt = executed["result"]["receipt"]
        stdout_ref = next(item for item in receipt["evidence_refs"] if item["kind"] == "apt_stdout")
        assert (tmp_path / stdout_ref["ref"]).read_bytes() == raw_stdout
        unsigned_receipt = dict(receipt)
        receipt_signature = unsigned_receipt.pop("signature")
        assert harness.auth.verify_bytes(
            canonical_json_bytes(unsigned_receipt), receipt_signature, domain=RECEIPT_DOMAIN
        )
        mutated_receipt = json.loads(json.dumps(unsigned_receipt))
        mutated_stdout_ref = next(
            item for item in mutated_receipt["evidence_refs"] if item["kind"] == "apt_stdout"
        )
        mutated_stdout_ref["sha256"] = "f" * 64
        mutated_stdout_ref["ref"] = f"evidence/{'f' * 64}.stdout"
        assert not harness.auth.verify_bytes(
            canonical_json_bytes(mutated_receipt), receipt_signature, domain=RECEIPT_DOMAIN
        )

        projection = canonical_json_bytes(executed)
        assert raw_stdout not in projection
        row = harness.store._connection.execute(  # noqa: SLF001
            "SELECT plan_json, receipt_json FROM broker_plans"
        ).fetchone()
        assert row is not None
        assert raw_stdout not in bytes(row["plan_json"])
        assert raw_stdout not in bytes(row["receipt_json"])
        event_codes = harness.store._connection.execute(  # noqa: SLF001
            "SELECT event_code FROM broker_events"
        ).fetchall()
        assert all(raw_stdout not in str(item["event_code"]).encode() for item in event_codes)
        assert raw_stdout not in repr(executed).encode()
    finally:
        harness.close()


def test_crash_after_apt_effect_reconciles_exact_poststate_without_second_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "broker.sqlite3"
    backend = FakeBackend()
    first = Harness(backend=backend, store=BrokerStore(database))
    try:
        planned = first.request("PlanInstall", first.plan_body(), idempotency_key="plan-1")
        assert isinstance(planned, dict)

        class SimulatedCrash(BaseException):
            pass

        def crash_before_final_receipt(*_args: object, **_kwargs: object) -> None:
            raise SimulatedCrash

        monkeypatch.setattr(first.store, "finish_execution", crash_before_final_receipt)
        with pytest.raises(SimulatedCrash):
            first.request(
                "ExecuteInstall",
                {"plan_id": planned["result"]["plan_id"]},
                idempotency_key="execute-1",
                plan_digest=planned["result"]["plan_digest"],
                approval_receipt_id="approval-1",
            )
        approval_proof = first.last_approval_proof
        assert approval_proof is not None
        assert backend.execute_calls == 1
        assert backend.installed is True
    finally:
        first.close()

    restarted = Harness(backend=backend, store=BrokerStore(database))
    try:
        status = restarted.request(
            "Status",
            {"plan_id": planned["result"]["plan_id"]},
            idempotency_key="status-1",
        )
        assert isinstance(status, dict)
        assert status["result"]["status"] == "unknown"
        assert status["result"]["error_code"] == "broker_restart_after_effect_claim"
        assert status["result"]["receipt"] is None

        reconciled = restarted.request(
            "ReconcileAfterRestart",
            {"plan_id": planned["result"]["plan_id"]},
            idempotency_key="reconcile-1",
            plan_digest=planned["result"]["plan_digest"],
        )
        assert isinstance(reconciled, dict) and reconciled["ok"] is True
        evidence = PackageReconciliationReceipt.from_payload(reconciled["result"]["reconciliation"])
        assert evidence.transaction_outcome is TransactionOutcome.UNKNOWN
        assert evidence.postcondition_state is PackagePostconditionState.DESIRED
        assert evidence.postcondition_satisfied is True
        assert evidence.safe_to_replan is False
        assert restarted.auth.verify_bytes(
            evidence.canonical_bytes_for_signing(),
            evidence.signature,
            domain=RECONCILIATION_DOMAIN,
        )
        assert backend.execute_calls == 1
        assert backend.reconcile_calls == 1

        repeated = restarted.request(
            "ReconcileAfterRestart",
            {"plan_id": planned["result"]["plan_id"]},
            idempotency_key="reconcile-1",
            plan_digest=planned["result"]["plan_digest"],
        )
        assert isinstance(repeated, dict) and repeated["result"]["idempotent"] is True
        assert repeated["result"]["reconciliation"] == reconciled["result"]["reconciliation"]
        assert backend.reconcile_calls == 1

        execution_retry = restarted.request(
            "ExecuteInstall",
            {"plan_id": planned["result"]["plan_id"]},
            idempotency_key="execute-1",
            plan_digest=planned["result"]["plan_digest"],
            approval_receipt_id="approval-1",
            approval_proof=approval_proof,
        )
        assert isinstance(execution_retry, dict)
        assert execution_retry["result"]["status"] == "unknown"
        assert backend.execute_calls == 1
    finally:
        restarted.close()


def test_restart_reconciliation_mixed_state_stays_durable_unknown_and_actor_bound(
    tmp_path: Path,
) -> None:
    database = tmp_path / "broker.sqlite3"
    first = Harness(store=BrokerStore(database))
    try:
        planned = first.request("PlanInstall", first.plan_body(), idempotency_key="plan-1")
        assert isinstance(planned, dict)
        record = first.store.get(planned["result"]["plan_id"], actor_user_id="owner", actor_own_id="own-1")
        first.store.claim_execution(
            record.plan.plan_id,
            actor_user_id="owner",
            actor_own_id="own-1",
            plan_digest=record.plan.digest,
            execution_idempotency_key="execute-1",
            transaction_id="apttxn_0123456789abcdef0123456789abcdef",
            approval_receipt_id="approval-1",
            approval_proof_id="approvalproof_0123456789abcdef0123456789abcdef",
            approval_proof_digest="d" * 64,
            now=1_000,
        )
    finally:
        first.close()

    backend = FakeBackend()
    backend.reconciliation_state = PackagePostconditionState.MIXED
    restarted = Harness(backend=backend, store=BrokerStore(database))
    try:
        forged = restarted.request(
            "ReconcileAfterRestart",
            {"plan_id": planned["result"]["plan_id"]},
            idempotency_key="reconcile-forged",
            plan_digest=planned["result"]["plan_digest"],
            actor_id="other",
        )
        assert isinstance(forged, dict)
        assert forged["result"]["error_code"] == "plan_not_found"
        assert backend.reconcile_calls == 0

        reconciled = restarted.request(
            "ReconcileAfterRestart",
            {"plan_id": planned["result"]["plan_id"]},
            idempotency_key="reconcile-1",
            plan_digest=planned["result"]["plan_digest"],
        )
        assert isinstance(reconciled, dict)
        evidence = PackageReconciliationReceipt.from_payload(reconciled["result"]["reconciliation"])
        assert evidence.postcondition_state is PackagePostconditionState.MIXED
        assert evidence.postcondition_satisfied is False
        assert evidence.safe_to_replan is False
        assert reconciled["result"]["status"] == "unknown"
        assert reconciled["result"]["error_code"] == "broker_restart_after_effect_claim"
        durable = restarted.store.get(
            planned["result"]["plan_id"], actor_user_id="owner", actor_own_id="own-1"
        )
        assert durable.status.value == "unknown"
        assert durable.reconciliation == evidence
    finally:
        restarted.close()


def test_execution_requires_approval_and_fresh_plan_digest_match() -> None:
    harness = Harness()
    try:
        planned = harness.request("PlanInstall", harness.plan_body(), idempotency_key="plan-1")
        assert isinstance(planned, dict)
        plan_id = planned["result"]["plan_id"]
        digest = planned["result"]["plan_digest"]
        missing = harness.request(
            "ExecuteInstall",
            {"plan_id": plan_id},
            idempotency_key="execute-1",
            plan_digest=digest,
        )
        assert isinstance(missing, dict)
        assert missing["result"]["error_code"] == "approval_required"

        harness.backend.drift = True
        drifted = harness.request(
            "ExecuteInstall",
            {"plan_id": plan_id},
            idempotency_key="execute-2",
            plan_digest=digest,
            approval_receipt_id="approval-1",
        )
        assert isinstance(drifted, dict)
        assert drifted["result"]["error_code"] == "plan_drift"
        assert harness.backend.execute_calls == 0
    finally:
        harness.close()


def test_forged_approval_id_without_backend_proof_is_rejected_before_apt() -> None:
    harness = Harness()
    try:
        planned = harness.request("PlanInstall", harness.plan_body(), idempotency_key="plan-1")
        assert isinstance(planned, dict)
        rejected = harness.request(
            "ExecuteInstall",
            {"plan_id": planned["result"]["plan_id"]},
            idempotency_key="execute-1",
            plan_digest=planned["result"]["plan_digest"],
            approval_receipt_id="approval-forged",
            auto_approval_proof=False,
        )
        assert isinstance(rejected, dict)
        assert rejected["result"]["error_code"] == "approval_proof_required"
        forged = PackageApprovalSigner(b"F" * 32).issue(
            broker_id="test-broker",
            approval_receipt_id="approval-forged",
            approval_payload_digest="d" * 64,
            plan_id=planned["result"]["plan_id"],
            plan_digest=planned["result"]["plan_digest"],
            actor_user_id="owner",
            actor_own_id="own-1",
            continuation_work_item_id="work-1",
            execution_idempotency_key="execute-2",
            issued_at=1_000,
            expires_at=1_100,
        )
        rejected_signature = harness.request(
            "ExecuteInstall",
            {"plan_id": planned["result"]["plan_id"]},
            idempotency_key="execute-2",
            plan_digest=planned["result"]["plan_digest"],
            approval_receipt_id="approval-forged",
            approval_proof=forged,
        )
        assert isinstance(rejected_signature, dict)
        assert rejected_signature["result"]["error_code"] == "approval_signature_invalid"
        assert harness.backend.execute_calls == 0
    finally:
        harness.close()


@pytest.mark.parametrize(
    ("claim_overrides", "request_overrides", "expected"),
    [
        (
            {"plan_id": "aptplan_fedcba9876543210fedcba9876543210"},
            {},
            "approval_binding_mismatch",
        ),
        ({"plan_digest": "e" * 64}, {}, "approval_binding_mismatch"),
        ({"actor_user_id": "other"}, {}, "approval_binding_mismatch"),
        ({"actor_own_id": "own-2"}, {}, "approval_binding_mismatch"),
        ({"continuation_work_item_id": "work-2"}, {}, "approval_binding_mismatch"),
        ({"execution_idempotency_key": "execute-2"}, {}, "approval_binding_mismatch"),
        ({"approval_payload_digest": "e" * 64}, {}, "approval_binding_mismatch"),
        ({"issued_at": 800, "expires_at": 900}, {}, "approval_expired"),
    ],
)
def test_signed_approval_proof_is_exactly_bound_and_expiring(
    claim_overrides: dict[str, Any],
    request_overrides: dict[str, Any],
    expected: str,
) -> None:
    harness = Harness()
    try:
        planned = harness.request("PlanInstall", harness.plan_body(), idempotency_key="plan-1")
        assert isinstance(planned, dict)
        claims: dict[str, Any] = {
            "broker_id": "test-broker",
            "approval_receipt_id": "approval-1",
            "approval_payload_digest": harness.approval_payload_digest(planned["result"]["plan_id"]),
            "plan_id": planned["result"]["plan_id"],
            "plan_digest": planned["result"]["plan_digest"],
            "actor_user_id": "owner",
            "actor_own_id": "own-1",
            "continuation_work_item_id": "work-1",
            "execution_idempotency_key": "execute-1",
            "issued_at": 1_000,
            "expires_at": 1_100,
        }
        claims.update(claim_overrides)
        proof = harness.approval_signer.issue(**claims)
        request_values = {
            "idempotency_key": "execute-1",
            "actor_id": "owner",
            "own_id": "own-1",
            "job_id": "work-1",
        }
        request_values.update(request_overrides)
        rejected = harness.request(
            "ExecuteInstall",
            {"plan_id": planned["result"]["plan_id"]},
            plan_digest=planned["result"]["plan_digest"],
            approval_receipt_id="approval-1",
            approval_proof=proof,
            **request_values,
        )
        assert isinstance(rejected, dict)
        assert rejected["result"]["error_code"] == expected
        assert harness.backend.execute_calls == 0
    finally:
        harness.close()


def test_consumed_proof_allows_only_exact_idempotent_retry() -> None:
    harness = Harness()
    try:
        planned = harness.request("PlanInstall", harness.plan_body(), idempotency_key="plan-1")
        assert isinstance(planned, dict)
        first = harness.request(
            "ExecuteInstall",
            {"plan_id": planned["result"]["plan_id"]},
            idempotency_key="execute-1",
            plan_digest=planned["result"]["plan_digest"],
            approval_receipt_id="approval-1",
        )
        assert isinstance(first, dict) and first["ok"] is True
        assert harness.last_approval_proof is not None
        expired_exact = harness.request(
            "ExecuteInstall",
            {"plan_id": planned["result"]["plan_id"]},
            idempotency_key="execute-1",
            plan_digest=planned["result"]["plan_digest"],
            approval_receipt_id="approval-1",
            approval_proof=harness.last_approval_proof,
            now=1_101,
        )
        assert isinstance(expired_exact, dict) and expired_exact["ok"] is True
        assert expired_exact["result"] == first["result"] | {"idempotent": True}
        assert harness.backend.execute_calls == 1
        replay = harness.request(
            "ExecuteInstall",
            {"plan_id": planned["result"]["plan_id"]},
            idempotency_key="execute-2",
            plan_digest=planned["result"]["plan_digest"],
            approval_receipt_id="approval-1",
            approval_proof=harness.last_approval_proof,
        )
        assert isinstance(replay, dict)
        assert replay["result"]["error_code"] == "approval_binding_mismatch"
        assert harness.backend.execute_calls == 1
    finally:
        harness.close()


def test_cancel_before_claim_is_final_and_actor_scope_does_not_leak_plan() -> None:
    harness = Harness()
    try:
        planned = harness.request("PlanInstall", harness.plan_body(), idempotency_key="plan-1")
        assert isinstance(planned, dict)
        plan_id = planned["result"]["plan_id"]
        digest = planned["result"]["plan_digest"]
        hidden = harness.request(
            "Status",
            {"plan_id": plan_id},
            idempotency_key="status-other",
            actor_id="other",
        )
        assert isinstance(hidden, dict)
        assert hidden["result"]["error_code"] == "plan_not_found"

        cancelled = harness.request("CancelBeforeCommit", {"plan_id": plan_id}, idempotency_key="cancel-1")
        assert isinstance(cancelled, dict)
        assert cancelled["result"]["status"] == "cancelled_before_commit"
        refused = harness.request(
            "ExecuteInstall",
            {"plan_id": plan_id},
            idempotency_key="execute-1",
            plan_digest=digest,
            approval_receipt_id="approval-1",
        )
        assert isinstance(refused, dict)
        assert refused["result"]["error_code"] == "plan_not_executable"
    finally:
        harness.close()


def test_no_generic_command_or_repository_method_and_replay_is_rejected() -> None:
    harness = Harness()
    try:
        forbidden = harness.request(
            "RunCommand", {"argv": ["apt", "install", "nmap"]}, idempotency_key="bad-1"
        )
        assert isinstance(forbidden, dict)
        assert forbidden["result"]["error_code"] == "unknown_method"

        raw = harness.request("Health", {}, idempotency_key="health-1", raw_only=True)
        assert isinstance(raw, bytes)
        first = json.loads(harness.daemon.handle_request(raw, peer_uid=1000, now=1_000))
        second = json.loads(harness.daemon.handle_request(raw, peer_uid=1000, now=1_000))
        assert first["ok"] is True
        assert second["result"]["error_code"] == "replayed_request"
    finally:
        harness.close()


def test_changed_plan_retry_conflicts_before_replanning() -> None:
    harness = Harness()
    try:
        first = harness.request("PlanInstall", harness.plan_body(), idempotency_key="plan-1")
        assert isinstance(first, dict) and first["ok"] is True
        changed = harness.request("PlanInstall", harness.plan_body(task="task-2"), idempotency_key="plan-1")
        assert isinstance(changed, dict)
        assert changed["result"]["error_code"] == "idempotency_conflict"
        assert harness.backend.plan_calls == 1
    finally:
        harness.close()


def test_draining_server_rejects_before_dispatch_with_a_bound_response(monkeypatch) -> None:
    class Reader:
        def __init__(self, raw: bytes) -> None:
            self._raw = raw

        async def readline(self) -> bytes:
            return self._raw + b"\n"

    class Writer:
        def __init__(self) -> None:
            self.output = bytearray()

        def write(self, value: bytes) -> None:
            self.output.extend(value)

        async def drain(self) -> None:
            return None

        def close(self) -> None:
            return None

        async def wait_closed(self) -> None:
            return None

    harness = Harness()
    try:
        raw = harness.request("Health", {}, idempotency_key="health-drain", raw_only=True)
        assert isinstance(raw, bytes)
        writer = Writer()
        harness.daemon._accepting_requests = False
        monkeypatch.setattr(daemon_module, "_peer_uid", lambda _writer: 1000)
        asyncio.run(harness.daemon._serve_client(Reader(raw), writer))  # type: ignore[arg-type]
        response = json.loads(bytes(writer.output[:-1]))
        signature = response.pop("signature")
        assert response["request_id"] == "request-1"
        assert response["result"] == {"error_code": "broker_draining"}
        assert harness.auth.verify_bytes(canonical_json_bytes(response), signature, domain=RESPONSE_DOMAIN)
        assert harness.backend.plan_calls == 0
        assert harness.daemon._clients_drained.is_set()
    finally:
        harness.close()
