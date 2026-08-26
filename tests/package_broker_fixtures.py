from __future__ import annotations

import hashlib

from friday.host_control.contracts import PROTOCOL_VERSION
from friday_package_broker.contracts import (
    BROKER_RECEIPT_SCHEMA_VERSION,
    AptInstallPlan,
    AptTransaction,
    InstalledPackage,
    PackageAction,
    PackageChange,
    PackageEvidenceReference,
    PackageRef,
    PackageTransactionReceipt,
    RepositoryOrigin,
    TransactionOutcome,
)


def trusted_origin(*, site: str = "archive.ubuntu.com") -> RepositoryOrigin:
    return RepositoryOrigin(
        origin="Ubuntu",
        label="Ubuntu",
        archive="noble",
        site=site,
        component="main",
        trusted=True,
    )


def transaction(*, site: str = "archive.ubuntu.com") -> AptTransaction:
    return AptTransaction(
        schema_version=1,
        requested=(PackageRef("nmap", "7.94", "amd64"),),
        changes=(
            PackageChange(
                action=PackageAction.INSTALL,
                name="nmap",
                architecture="amd64",
                from_version=None,
                to_version="7.94",
                download_bytes=1024,
                installed_delta_bytes=4096,
                archive_sha256="c" * 64,
                origins=(trusted_origin(site=site),),
            ),
        ),
        download_bytes=1024,
        installed_delta_bytes=4096,
    )


def plan(*, plan_id: str = "aptplan_0123456789abcdef0123456789abcdef") -> AptInstallPlan:
    return AptInstallPlan(
        schema_version=1,
        plan_id=plan_id,
        broker_id="test-broker",
        actor_user_id="owner",
        actor_own_id="own-1",
        original_task_ref="task-1",
        continuation_work_item_id="work-1",
        transaction=transaction(),
        created_at=1_000,
        expires_at=1_900,
    )


def receipt(
    package_plan: AptInstallPlan,
    *,
    signature: str = "a" * 128,
) -> PackageTransactionReceipt:
    empty_digest = hashlib.sha256(b"").hexdigest()
    evidence_digest = "e" * 64
    return PackageTransactionReceipt(
        schema_version=BROKER_RECEIPT_SCHEMA_VERSION,
        protocol_version=PROTOCOL_VERSION,
        broker_id="test-broker",
        broker_build_id="test-build",
        package_manager="apt",
        package_manager_version="2.8.0",
        transaction_id="apttxn_0123456789abcdef0123456789abcdef",
        plan_id=package_plan.plan_id,
        approved_plan_digest=package_plan.digest,
        executed_transaction_digest=package_plan.transaction.digest,
        approval_receipt_id="approval-1",
        idempotency_key="execute-1",
        outcome=TransactionOutcome.COMPLETED,
        effect_boundary_crossed=True,
        started_at=1_100,
        finished_at=1_101,
        exit_code=0,
        lock_state="released",
        before=(),
        after=(InstalledPackage("nmap", "7.94", "amd64"),),
        output_capture_status="captured",
        stdout_sha256=empty_digest,
        stdout_size_bytes=0,
        stderr_sha256=empty_digest,
        stderr_size_bytes=0,
        output_truncated=False,
        reboot_required=False,
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
        signature=signature,
    )
