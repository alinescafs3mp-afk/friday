from __future__ import annotations

from dataclasses import replace

import pytest

from friday.host_control.contracts import PROTOCOL_VERSION
from friday_package_broker.contracts import (
    InstalledPackage,
    PackagePostconditionState,
    PackageReconciliationReceipt,
    TransactionOutcome,
)
from friday_package_broker.store import (
    BrokerStore,
    PlanStatus,
    StoreConflict,
    StoreNotFound,
    StoreStateError,
)
from tests.package_broker_fixtures import plan, receipt


def reconciliation(package_plan, *, idempotency_key: str = "reconcile-1"):
    return PackageReconciliationReceipt(
        schema_version=1,
        protocol_version=PROTOCOL_VERSION,
        broker_id="test-broker",
        broker_build_id="test-build",
        reconciliation_id="aptrecon_0123456789abcdef0123456789abcdef",
        transaction_id="apttxn_0123456789abcdef0123456789abcdef",
        plan_id=package_plan.plan_id,
        plan_digest=package_plan.digest,
        transaction_digest=package_plan.transaction.digest,
        approval_receipt_id="approval-1",
        actor_user_id="owner",
        actor_own_id="own-1",
        continuation_work_item_id="work-1",
        reconciliation_idempotency_key=idempotency_key,
        transaction_outcome=TransactionOutcome.UNKNOWN,
        postcondition_state=PackagePostconditionState.DESIRED,
        postcondition_satisfied=True,
        safe_to_replan=False,
        observed_at=1_200,
        installed=(InstalledPackage("nmap", "7.94", "amd64"),),
        signature="a" * 128,
    )


def test_plan_idempotency_is_exact_and_actor_scoped(tmp_path) -> None:
    store = BrokerStore(tmp_path / "broker.sqlite3")
    package_plan = plan()
    try:
        first = store.save_plan(package_plan, request_digest="a" * 64, idempotency_key="plan-1")
        exact = store.idempotent_plan(
            idempotency_key="plan-1",
            request_digest="a" * 64,
            actor_user_id="owner",
            actor_own_id="own-1",
        )
        assert exact == first
        with pytest.raises(StoreConflict):
            store.idempotent_plan(
                idempotency_key="plan-1",
                request_digest="b" * 64,
                actor_user_id="owner",
                actor_own_id="own-1",
            )
        with pytest.raises(StoreNotFound):
            store.get(package_plan.plan_id, actor_user_id="other", actor_own_id="own-1")
    finally:
        store.close()


def test_cancel_wins_only_before_execution_claim(tmp_path) -> None:
    store = BrokerStore(tmp_path / "broker.sqlite3")
    package_plan = plan()
    try:
        store.save_plan(package_plan, request_digest="a" * 64, idempotency_key="plan-1")
        cancelled = store.cancel_before_commit(
            package_plan.plan_id, actor_user_id="owner", actor_own_id="own-1", now=1_100
        )
        assert cancelled.status is PlanStatus.CANCELLED_BEFORE_COMMIT
        with pytest.raises(StoreStateError, match="plan_not_executable"):
            store.claim_execution(
                package_plan.plan_id,
                actor_user_id="owner",
                actor_own_id="own-1",
                plan_digest=package_plan.digest,
                execution_idempotency_key="execute-1",
                transaction_id="apttxn_0123456789abcdef0123456789abcdef",
                approval_receipt_id="approval-1",
                approval_proof_id="approvalproof_0123456789abcdef0123456789abcdef",
                approval_proof_digest="d" * 64,
                now=1_101,
            )
    finally:
        store.close()


def test_restart_marks_claimed_transaction_unknown_and_never_reopens_it(tmp_path) -> None:
    database = tmp_path / "broker.sqlite3"
    package_plan = plan()
    store = BrokerStore(database)
    store.save_plan(package_plan, request_digest="a" * 64, idempotency_key="plan-1")
    claim = store.claim_execution(
        package_plan.plan_id,
        actor_user_id="owner",
        actor_own_id="own-1",
        plan_digest=package_plan.digest,
        execution_idempotency_key="execute-1",
        transaction_id="apttxn_0123456789abcdef0123456789abcdef",
        approval_receipt_id="approval-1",
        approval_proof_id="approvalproof_0123456789abcdef0123456789abcdef",
        approval_proof_digest="d" * 64,
        now=1_100,
    )
    assert claim.should_execute is True
    store.close()

    recovered = BrokerStore(database)
    try:
        record = recovered.get(package_plan.plan_id, actor_user_id="owner", actor_own_id="own-1")
        assert record.status is PlanStatus.UNKNOWN
        assert record.error_code == "broker_restart_after_effect_claim"
        assert record.transaction_id == "apttxn_0123456789abcdef0123456789abcdef"
        assert record.execution_started_at == 1_100
        repeated = recovered.claim_execution(
            package_plan.plan_id,
            actor_user_id="owner",
            actor_own_id="own-1",
            plan_digest=package_plan.digest,
            execution_idempotency_key="execute-1",
            transaction_id="apttxn_0123456789abcdef0123456789abcdef",
            approval_receipt_id="approval-1",
            approval_proof_id="approvalproof_0123456789abcdef0123456789abcdef",
            approval_proof_digest="d" * 64,
            now=1_200,
        )
        assert repeated.should_execute is False
        assert repeated.record.status is PlanStatus.UNKNOWN
    finally:
        recovered.close()


def test_restart_reconciliation_is_durable_actor_scoped_and_idempotent(tmp_path) -> None:
    database = tmp_path / "broker.sqlite3"
    package_plan = plan()
    store = BrokerStore(database)
    store.save_plan(package_plan, request_digest="a" * 64, idempotency_key="plan-1")
    store.claim_execution(
        package_plan.plan_id,
        actor_user_id="owner",
        actor_own_id="own-1",
        plan_digest=package_plan.digest,
        execution_idempotency_key="execute-1",
        transaction_id="apttxn_0123456789abcdef0123456789abcdef",
        approval_receipt_id="approval-1",
        approval_proof_id="approvalproof_0123456789abcdef0123456789abcdef",
        approval_proof_digest="d" * 64,
        now=1_100,
    )
    store.close()

    recovered = BrokerStore(database)
    evidence = reconciliation(package_plan)
    try:
        saved = recovered.save_reconciliation(
            package_plan.plan_id,
            actor_user_id="owner",
            actor_own_id="own-1",
            plan_digest=package_plan.digest,
            idempotency_key="reconcile-1",
            receipt=evidence,
            now=1_200,
        )
        assert saved.status is PlanStatus.UNKNOWN
        assert saved.error_code == "broker_restart_after_effect_claim"
        assert saved.reconciliation == evidence
        assert (
            recovered.idempotent_reconciliation(
                package_plan.plan_id,
                actor_user_id="owner",
                actor_own_id="own-1",
                plan_digest=package_plan.digest,
                idempotency_key="reconcile-1",
            )
            == saved
        )
        with pytest.raises(StoreConflict, match="reconciliation_idempotency_conflict"):
            recovered.idempotent_reconciliation(
                package_plan.plan_id,
                actor_user_id="owner",
                actor_own_id="own-1",
                plan_digest=package_plan.digest,
                idempotency_key="reconcile-2",
            )
        with pytest.raises(StoreNotFound):
            recovered.idempotent_reconciliation(
                package_plan.plan_id,
                actor_user_id="other",
                actor_own_id="own-1",
                plan_digest=package_plan.digest,
                idempotency_key="reconcile-1",
            )
    finally:
        recovered.close()


def test_new_effect_cannot_persist_a_legacy_execution_receipt(tmp_path) -> None:
    store = BrokerStore(tmp_path / "broker.sqlite3")
    package_plan = plan()
    store.save_plan(package_plan, request_digest="a" * 64, idempotency_key="plan-1")
    store.claim_execution(
        package_plan.plan_id,
        actor_user_id="owner",
        actor_own_id="own-1",
        plan_digest=package_plan.digest,
        execution_idempotency_key="execute-1",
        transaction_id="apttxn_0123456789abcdef0123456789abcdef",
        approval_receipt_id="approval-1",
        approval_proof_id="approvalproof_0123456789abcdef0123456789abcdef",
        approval_proof_digest="d" * 64,
        now=1_100,
    )
    current = receipt(package_plan)
    manifest = next(item for item in current.evidence_refs if item.kind == "apt_dpkg_transaction")
    legacy = replace(
        current,
        schema_version=2,
        stdout_total_size_bytes=None,
        stderr_total_size_bytes=None,
        stdout_total_size_complete=False,
        stderr_total_size_complete=False,
        evidence_refs=(manifest,),
    )
    try:
        with pytest.raises(ValueError, match="current receipt schema"):
            store.finish_execution(
                package_plan.plan_id,
                receipt=legacy,
                status=PlanStatus.COMPLETED,
                error_code=None,
                now=1_101,
            )
    finally:
        store.close()


def test_terminal_receipt_is_durable_and_exact_execution_retry_does_not_claim(tmp_path) -> None:
    store = BrokerStore(tmp_path / "broker.sqlite3")
    package_plan = plan()
    try:
        store.save_plan(package_plan, request_digest="a" * 64, idempotency_key="plan-1")
        store.claim_execution(
            package_plan.plan_id,
            actor_user_id="owner",
            actor_own_id="own-1",
            plan_digest=package_plan.digest,
            execution_idempotency_key="execute-1",
            transaction_id="apttxn_0123456789abcdef0123456789abcdef",
            approval_receipt_id="approval-1",
            approval_proof_id="approvalproof_0123456789abcdef0123456789abcdef",
            approval_proof_digest="d" * 64,
            now=1_100,
        )
        terminal = store.finish_execution(
            package_plan.plan_id,
            receipt=receipt(package_plan),
            status=PlanStatus.COMPLETED,
            error_code=None,
            now=1_101,
        )
        assert terminal.receipt is not None
        repeated = store.claim_execution(
            package_plan.plan_id,
            actor_user_id="owner",
            actor_own_id="own-1",
            plan_digest=package_plan.digest,
            execution_idempotency_key="execute-1",
            transaction_id="apttxn_0123456789abcdef0123456789abcdef",
            approval_receipt_id="approval-1",
            approval_proof_id="approvalproof_0123456789abcdef0123456789abcdef",
            approval_proof_digest="d" * 64,
            now=1_200,
        )
        assert repeated.should_execute is False
        assert repeated.record.receipt == terminal.receipt
        with pytest.raises(StoreConflict):
            store.claim_execution(
                package_plan.plan_id,
                actor_user_id="owner",
                actor_own_id="own-1",
                plan_digest=package_plan.digest,
                execution_idempotency_key="execute-2",
                transaction_id="apttxn_fedcba9876543210fedcba9876543210",
                approval_receipt_id="approval-1",
                approval_proof_id="approvalproof_fedcba9876543210fedcba9876543210",
                approval_proof_digest="e" * 64,
                now=1_200,
            )
    finally:
        store.close()
