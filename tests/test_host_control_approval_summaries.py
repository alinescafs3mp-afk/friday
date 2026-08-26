from __future__ import annotations

from package_broker_fixtures import plan as package_plan

from friday.host_control.approval_summary import (
    host_action_approval_summary,
    package_install_approval_summary,
)
from friday.host_control.contracts import (
    ExecutionProfile,
    RiskClass,
    canonical_json_bytes,
)
from friday.host_control.plans import HostActionPlan
from friday.host_control.policy import NetworkTargetSnapshot, TargetBinding


def _network_plan() -> HostActionPlan:
    snapshot = NetworkTargetSnapshot(
        schema_version=1,
        policy_digest="a" * 64,
        bindings=(
            TargetBinding(
                requested="router.lan",
                execution_targets=("192.168.1.7",),
                resolved_addresses=("192.168.1.7",),
                address_count=1,
                classification="connected_private",
                route_evidence=("connected:192.168.1.0/24",),
                approval_required=True,
            ),
        ),
        target_count=1,
        approval_required=True,
    )
    return HostActionPlan(
        schema_version=1,
        plan_id="hplan_0123456789abcdef",
        actor_user_id="owner",
        actor_own_id="owner",
        conversation_id="conv_0123456789abcdef",
        source_message_id="msg_0123456789abcdef",
        continuation_work_item_id=None,
        host_agent_id="local-user-agent",
        idempotency_key="network-approval-summary",
        capability_id="network.nmap.scan",
        adapter_id="network.nmap",
        adapter_schema_version=1,
        implementation_version=1,
        adapter_digest="b" * 64,
        action_id="selected_ports",
        normalized_arguments_json=canonical_json_bytes(
            {
                "ports": [22, 443],
                "target_count": 1,
                "target_snapshot_digest": snapshot.digest,
                "targets": ["192.168.1.7"],
            }
        ),
        risk_class=RiskClass.NETWORK_OBSERVE,
        security_id="host.network.observe",
        execution_profile=ExecutionProfile.CLI_NETWORK_UNPRIVILEGED,
        timeout_sec=120,
        max_output_bytes=8 * 1024 * 1024,
        target_snapshot_json=canonical_json_bytes(snapshot.to_payload()),
        workspace_grants=(),
        executable_attestation_digest="c" * 64,
        created_at=1_000,
        expires_at=1_900,
    )


def test_package_approval_names_every_exact_effect_and_continuation() -> None:
    exact = package_plan()

    summary = package_install_approval_summary(
        exact,
        expected_capabilities=("network.nmap.scan",),
        original_request="Install nmap and scan my approved local subnet.",
    )

    assert "Package manager: APT" in summary
    assert "nmap=7.94:amd64" in summary
    assert "ADD nmap:amd64 ∅ -> 7.94" in summary
    assert "archive_sha256=" + "c" * 64 in summary
    assert "site=archive.ubuntu.com" in summary
    assert "Download bytes: 1024" in summary
    assert "Estimated disk change bytes: 4096" in summary
    assert "Services/units at plan time:" in summary
    assert "network.nmap.scan" in summary
    assert "Install nmap and scan my approved local subnet." in summary
    assert "Continuation work item: work-1" in summary
    assert f"Exact plan sha256: {exact.digest}" in summary


def test_network_approval_names_pinned_ip_ports_timeout_policy_and_coverage() -> None:
    plan = _network_plan()

    summary = host_action_approval_summary(plan)

    assert "requested=router.lan; pinned=192.168.1.7" in summary
    assert "classification=connected_private" in summary
    assert "ports=22,443" in summary
    assert "Expected coverage: account for all 1 pinned addresses" in summary
    assert "Timeout seconds: 120" in summary
    assert "Maximum captured output bytes: 8388608" in summary
    assert "Network policy digest: " + "a" * 64 in summary
    assert f"Exact plan sha256: {plan.digest}" in summary
