from dataclasses import replace

import pytest

from friday.orchestration.coding_worker_admission import (
    CodingWorkerAdmissionReason,
    CodingWorkerAdmissionState,
    build_coding_worker_admission,
)
from friday.orchestration.coding_worker_identity import (
    CodingWorkerIdentityState,
    CodingWorkerIdentityV1,
    build_coding_worker_identity,
)
from friday.orchestration.coding_worker_isolation import (
    CodingWorkerIsolationAdmissionV1,
    CodingWorkerIsolationState,
    build_coding_worker_isolation,
)
from friday.orchestration.coding_worker_limits import (
    CodingWorkerLimitsState,
    CodingWorkerLimitsV1,
    build_coding_worker_limits,
)
from friday.orchestration.coding_worker_network import (
    CodingWorkerNetworkPolicyV1,
    CodingWorkerNetworkState,
    build_coding_worker_network,
)
from friday.orchestration.coding_worker_workspace import (
    CodingWorkerWorkspaceState,
    CodingWorkerWorkspaceV1,
    build_coding_worker_workspace,
)

SNAPSHOT = "a" * 64


def components() -> tuple[
    CodingWorkerIdentityV1,
    CodingWorkerIsolationAdmissionV1,
    CodingWorkerNetworkPolicyV1,
    CodingWorkerWorkspaceV1,
    CodingWorkerLimitsV1,
]:
    return (
        build_coding_worker_identity(
            "identity:1",
            "turn:1",
            {"worker_id": "worker:1", "operation_id": "operation:1", "project_id": "project:1"},
        ),
        build_coding_worker_isolation(
            "isolation:1",
            "turn:1",
            {
                "host_secrets_visible": False,
                "docker_socket_present": False,
                "production_database_reachable": False,
                "owner_ssh_keys_visible": False,
            },
        ),
        build_coding_worker_network("network:1", "turn:1", {"policy": "disabled"}),
        build_coding_worker_workspace(
            "workspace:1",
            "turn:1",
            {
                "operation_id": "operation:1",
                "project_root": "/srv/project",
                "workspace_path": "work/1",
                "input_snapshot_sha256": SNAPSHOT,
                "export_path": "out",
            },
        ),
        build_coding_worker_limits(
            "limits:1", "turn:1", {"wall_clock_sec": 60, "memory_bytes": 1024, "cpu_sec": 30}
        ),
    )


def test_no_components_are_empty() -> None:
    result = build_coding_worker_admission("admission:1", "turn:1")

    assert result.admission is CodingWorkerAdmissionState.EMPTY
    assert result.reason is CodingWorkerAdmissionReason.NO_FACTS
    assert result.identity is None


def test_all_five_contracts_are_admitted() -> None:
    identity, isolation, network, workspace, limits = components()
    result = build_coding_worker_admission(
        "admission:1",
        "turn:1",
        identity=identity,
        isolation=isolation,
        network=network,
        workspace=workspace,
        limits=limits,
    )

    assert result.admission is CodingWorkerAdmissionState.ADMITTED
    assert result.reason is CodingWorkerAdmissionReason.ADMITTED
    assert result.identity is identity
    assert result.isolation is isolation
    assert result.network is network
    assert result.workspace is workspace
    assert result.limits is limits


def test_fact_mappings_are_composed_without_live_wiring() -> None:
    result = build_coding_worker_admission(
        "admission:1",
        "turn:1",
        {
            "identity": {"worker_id": "worker:1", "operation_id": "operation:1", "project_id": "project:1"},
            "isolation": {
                "host_secrets_visible": False,
                "docker_socket_present": False,
                "production_database_reachable": False,
                "owner_ssh_keys_visible": False,
            },
            "network": {"policy": "disabled"},
            "workspace": {
                "operation_id": "operation:1",
                "project_root": "/srv/project",
                "workspace_path": "work/1",
                "input_snapshot_sha256": SNAPSHOT,
                "export_path": "out",
            },
            "limits": {"wall_clock_sec": 60, "memory_bytes": 1024, "cpu_sec": 30},
        },
    )

    assert result.admission is CodingWorkerAdmissionState.ADMITTED


@pytest.mark.parametrize("index", range(5))
def test_any_blocked_component_blocks_and_redacts_the_composition(index: int) -> None:
    values = list(components())
    if index == 0:
        values[index] = build_coding_worker_identity(
            "identity:blocked",
            "turn:1",
            {
                "worker_id": "worker:1",
                "operation_id": "operation:1",
                "project_id": "project:1",
                "revision_selector": "latest",
            },
        )
    elif index == 1:
        values[index] = build_coding_worker_isolation(
            "isolation:blocked",
            "turn:1",
            {
                "host_secrets_visible": True,
                "docker_socket_present": False,
                "production_database_reachable": False,
                "owner_ssh_keys_visible": False,
            },
        )
    elif index == 2:
        values[index] = build_coding_worker_network("network:blocked", "turn:1", {"host_network": True})
    elif index == 3:
        values[index] = build_coding_worker_workspace(
            "workspace:blocked", "turn:1", {"workspace_path": "../out"}
        )
    else:
        values[index] = build_coding_worker_limits("limits:blocked", "turn:1", {"wall_clock_sec": 0})
    result = build_coding_worker_admission(
        "admission:1",
        "turn:1",
        identity=values[0],
        isolation=values[1],
        network=values[2],
        workspace=values[3],
        limits=values[4],
    )

    assert result.admission is CodingWorkerAdmissionState.BLOCKED
    assert result.identity is None
    assert result.workspace is None
    assert result.limits is None


def test_missing_component_is_blocked_and_serialized_result_has_no_components() -> None:
    identity, isolation, network, workspace, _ = components()
    result = build_coding_worker_admission(
        "admission:1", "turn:1", identity=identity, isolation=isolation, network=network, workspace=workspace
    )

    assert result.admission is CodingWorkerAdmissionState.BLOCKED
    assert result.reason is CodingWorkerAdmissionReason.MISSING_COMPONENT
    assert all(value is None for value in result.to_mapping().values() if isinstance(value, dict))


def test_only_the_allowed_network_states_admit() -> None:
    identity, isolation, _, workspace, limits = components()
    bounded = build_coding_worker_network(
        "network:bounded", "turn:1", {"policy": "bounded", "dependency_or_research_steps": ["dependency"]}
    )
    result = build_coding_worker_admission(
        "admission:1",
        "turn:1",
        identity=identity,
        isolation=isolation,
        network=bounded,
        workspace=workspace,
        limits=limits,
    )

    assert bounded.network is CodingWorkerNetworkState.BOUNDED
    assert result.admission is CodingWorkerAdmissionState.ADMITTED


def test_component_states_are_not_rewritten_by_composition() -> None:
    identity, isolation, network, workspace, limits = components()
    assert identity.identity is CodingWorkerIdentityState.IDENTIFIED
    assert isolation.isolation is CodingWorkerIsolationState.ADMITTED
    assert network.network is CodingWorkerNetworkState.DISABLED
    assert workspace.workspace is CodingWorkerWorkspaceState.BOUND
    assert limits.limits is CodingWorkerLimitsState.BOUNDED
    assert replace(identity, worker_id="worker:2").worker_id == "worker:2"


def test_component_turn_or_operation_mismatch_blocks() -> None:
    identity, isolation, network, workspace, limits = components()
    mismatched_turn = build_coding_worker_limits(
        "limits:other", "turn:other", {"wall_clock_sec": 60, "memory_bytes": 1024, "cpu_sec": 30}
    )
    mismatched_operation = build_coding_worker_workspace(
        "workspace:other",
        "turn:1",
        {
            "operation_id": "operation:other",
            "project_root": "/srv/project",
            "workspace_path": "work/1",
            "input_snapshot_sha256": SNAPSHOT,
            "export_path": "out",
        },
    )

    turn_result = build_coding_worker_admission(
        "admission:1",
        "turn:1",
        identity=identity,
        isolation=isolation,
        network=network,
        workspace=workspace,
        limits=mismatched_turn,
    )
    operation_result = build_coding_worker_admission(
        "admission:1",
        "turn:1",
        identity=identity,
        isolation=isolation,
        network=network,
        workspace=mismatched_operation,
        limits=limits,
    )

    assert turn_result.reason is CodingWorkerAdmissionReason.AUTHENTICATED_TURN_MISMATCH
    assert operation_result.reason is CodingWorkerAdmissionReason.OPERATION_MISMATCH
