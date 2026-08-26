from __future__ import annotations

import ast
import inspect
from dataclasses import replace

import pytest

import friday_package_broker.contracts as contracts_module
from friday_package_broker.contracts import (
    BROKER_RECONCILIATION_SCHEMA_VERSION,
    AptInstallPlan,
    BrokerContractError,
    InstalledPackage,
    PackageAction,
    PackageChange,
    PackageEvidenceReference,
    PackagePostconditionState,
    PackageReconciliationReceipt,
    PackageRef,
    PackageTransactionReceipt,
    ServiceUnitChange,
    ServiceUnitObservation,
    ServiceUnitState,
    TransactionOutcome,
)
from friday_package_broker.policy import BrokerPolicy
from tests.package_broker_fixtures import plan, transaction, trusted_origin


@pytest.mark.parametrize(
    "name",
    (
        "https://example.invalid/p.deb",
        "./local.deb",
        "/tmp/local.deb",
        "nmap;shutdown",
        "nmap $(id)",
        "nmap:amd64",
    ),
)
def test_package_refs_cannot_encode_urls_files_shell_or_apt_qualifiers(name: str) -> None:
    with pytest.raises(BrokerContractError):
        PackageRef(name)


def test_plan_has_an_exact_canonical_inverse_and_digest() -> None:
    package_plan = plan()

    recovered = AptInstallPlan.from_canonical_bytes(package_plan.canonical_bytes())

    assert recovered == package_plan
    assert recovered.digest == package_plan.digest
    assert recovered.to_payload()["transaction_digest"] == package_plan.transaction.digest


def test_plan_rejects_a_transaction_digest_substitution() -> None:
    payload = plan().to_payload()
    payload["transaction_digest"] = "f" * 64

    with pytest.raises(BrokerContractError, match="does not match"):
        AptInstallPlan.from_payload(payload)


def test_policy_is_default_deny_and_requires_every_origin_to_be_authenticated() -> None:
    policy = BrokerPolicy(
        broker_id="test-broker",
        allowed_peer_uids=frozenset({1000}),
        allowed_packages=frozenset({"nmap"}),
    )
    policy.authorize(transaction())
    untrusted = replace(trusted_origin(), trusted=False)
    changed = replace(transaction().changes[0], origins=(untrusted,))

    with pytest.raises(BrokerContractError, match="authenticated"):
        policy.authorize(replace(transaction(), changes=(changed,)))
    with pytest.raises(BrokerContractError, match="outside"):
        policy.authorize_requested((PackageRef("curl"),))


@pytest.mark.parametrize("action", (PackageAction.REMOVE, PackageAction.DOWNGRADE))
def test_initial_policy_forbids_remove_and_downgrade(action: PackageAction) -> None:
    policy = BrokerPolicy(
        broker_id="test-broker",
        allowed_peer_uids=frozenset({1000}),
        allowed_packages=frozenset({"nmap"}),
    )
    base = transaction().changes[0]
    if action is PackageAction.REMOVE:
        change = PackageChange(
            action=action,
            name="nmap",
            architecture="amd64",
            from_version="7.94",
            to_version=None,
            download_bytes=0,
            installed_delta_bytes=-4096,
            archive_sha256=None,
            origins=(),
        )
    else:
        change = replace(base, action=action, from_version="7.95", to_version="7.94")

    with pytest.raises(BrokerContractError, match="forbids"):
        policy.authorize(replace(transaction(), changes=(change,)))


def test_receipt_contract_has_no_raw_output_or_command_field() -> None:
    from tests.package_broker_fixtures import receipt

    payload = receipt(plan()).to_payload()

    assert "stdout" not in payload
    assert "stderr" not in payload
    assert "command" not in payload
    assert payload["package_manager"] == "apt"
    assert payload["output_capture_status"] == "captured"
    assert payload["stdout_size_bytes"] == 0
    assert {item["kind"] for item in payload["evidence_refs"]} == {
        "apt_dpkg_transaction",
        "apt_stderr",
        "apt_stdout",
    }
    assert payload["service_unit_observation_status"] == "captured"


def test_v3_completed_receipt_requires_content_addressed_bounded_raw_evidence() -> None:
    from tests.package_broker_fixtures import receipt

    with pytest.raises(BrokerContractError, match="lacks bounded APT evidence"):
        replace(receipt(plan()), evidence_refs=())
    with pytest.raises(BrokerContractError, match="content-addressed"):
        PackageEvidenceReference(
            kind="apt_dpkg_transaction",
            ref=f"evidence/{'a' * 64}.json",
            sha256="b" * 64,
            size_bytes=100,
        )
    with pytest.raises(BrokerContractError, match="completeness is contradictory"):
        replace(receipt(plan()), stdout_total_size_complete=False)
    with pytest.raises(BrokerContractError, match="total size is invalid"):
        replace(receipt(plan()), stdout_total_size_bytes=-1)


def test_package_receipt_source_cannot_silently_repeat_a_dataclass_field() -> None:
    source = ast.parse(inspect.getsource(contracts_module))
    receipt_class = next(
        node
        for node in source.body
        if isinstance(node, ast.ClassDef) and node.name == "PackageTransactionReceipt"
    )
    fields = [
        node.target.id
        for node in receipt_class.body
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
    ]

    assert len(fields) == len(set(fields)), "PackageTransactionReceipt repeats a dataclass field"


def test_reconciliation_is_separate_signed_state_evidence_without_effect_output() -> None:
    package_plan = plan()
    evidence = PackageReconciliationReceipt(
        schema_version=BROKER_RECONCILIATION_SCHEMA_VERSION,
        protocol_version="1.0",
        broker_id=package_plan.broker_id,
        broker_build_id="test-build",
        reconciliation_id="aptrecon_0123456789abcdef0123456789abcdef",
        transaction_id="apttxn_0123456789abcdef0123456789abcdef",
        plan_id=package_plan.plan_id,
        plan_digest=package_plan.digest,
        transaction_digest=package_plan.transaction.digest,
        approval_receipt_id="approval-1",
        actor_user_id=package_plan.actor_user_id,
        actor_own_id=package_plan.actor_own_id,
        continuation_work_item_id=package_plan.continuation_work_item_id,
        reconciliation_idempotency_key="reconcile-1",
        transaction_outcome=TransactionOutcome.UNKNOWN,
        postcondition_state=PackagePostconditionState.DESIRED,
        postcondition_satisfied=True,
        safe_to_replan=False,
        observed_at=1_200,
        installed=(InstalledPackage("nmap", "7.94", "amd64"),),
        signature="a" * 128,
    )

    payload = evidence.to_payload()
    assert PackageReconciliationReceipt.from_payload(payload) == evidence
    assert evidence.transaction_outcome is TransactionOutcome.UNKNOWN
    assert {
        "stdout",
        "stderr",
        "evidence_refs",
        "service_unit_observations",
        "effect_boundary_crossed",
    }.isdisjoint(payload)
    with pytest.raises(BrokerContractError, match="version"):
        replace(evidence, schema_version=0)
    with pytest.raises(BrokerContractError, match="contradictory"):
        replace(evidence, postcondition_satisfied=False)


def test_service_unit_observation_is_closed_bounded_and_round_trips() -> None:
    state = ServiceUnitState("loaded", "enabled", "active", "running", 123)
    observation = ServiceUnitObservation(
        package_name="nmap",
        package_architecture="amd64",
        unit_name="nmap-helper.service",
        before=None,
        after=state,
        changes=tuple(
            sorted(
                {
                    ServiceUnitChange.ENABLED,
                    ServiceUnitChange.NEWLY_PRESENT,
                    ServiceUnitChange.STARTED,
                },
                key=lambda item: item.value,
            )
        ),
    )

    assert ServiceUnitObservation.from_payload(observation.to_payload()) == observation
    with pytest.raises(BrokerContractError, match="unit name"):
        replace(observation, unit_name="../../attacker.service")


def test_legacy_v1_receipt_remains_verifiable_without_claiming_v2_evidence() -> None:
    from tests.package_broker_fixtures import receipt

    current = receipt(plan())
    legacy = replace(
        current,
        schema_version=1,
        output_capture_status="unavailable",
        stdout_sha256=None,
        stdout_size_bytes=None,
        stderr_sha256=None,
        stderr_size_bytes=None,
        stdout_total_size_bytes=None,
        stderr_total_size_bytes=None,
        stdout_total_size_complete=False,
        stderr_total_size_complete=False,
        evidence_refs=(),
        service_unit_observation_status="unavailable",
        service_unit_observations=(),
    )
    payload = legacy.to_payload()

    assert "evidence_refs" not in payload
    assert "service_unit_observations" not in payload
    recovered = PackageTransactionReceipt.from_payload(payload)
    assert recovered.schema_version == 1
    assert recovered.outcome is TransactionOutcome.COMPLETED


def test_legacy_v2_receipt_remains_verifiable_with_manifest_only_evidence() -> None:
    from tests.package_broker_fixtures import receipt

    current = receipt(plan())
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

    payload = legacy.to_payload()
    assert "stdout_total_size_bytes" not in payload
    assert PackageTransactionReceipt.from_payload(payload) == legacy
