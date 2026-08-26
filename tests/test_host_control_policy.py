from __future__ import annotations

import pytest

from friday.host_control.contracts import ContractError
from friday.host_control.policy import (
    NetworkPolicy,
    assert_target_snapshot_current,
    normalize_network_targets,
)


def policy(**changes):  # noqa: ANN003, ANN201
    values = {
        "connected_cidrs": ("192.168.1.0/24", "10.0.0.0/24", "fe80::/120"),
        "allowed_cidrs": (),
        "allow_public": False,
        "max_targets": 256,
    }
    values.update(changes)
    return NetworkPolicy(**values)


def test_exact_connected_private_host_and_local_cidr_are_pinned() -> None:
    one = normalize_network_targets(["192.168.1.7"], policy())
    assert one.execution_targets == ("192.168.1.7",)
    assert one.target_count == 1
    subnet = normalize_network_targets(["192.168.1.0/24"], policy())
    assert subnet.execution_targets == ("192.168.1.0/24",)
    assert subnet.target_count == 256
    assert subnet.approval_required is False


@pytest.mark.parametrize(
    "target",
    [
        "169.254.169.254",
        "169.254.0.0/16",
        "0.0.0.0",
        "224.0.0.1",
        "198.18.0.1",
        "::ffff:169.254.169.254",
        "ff02::1",
    ],
)
def test_special_use_targets_are_explicitly_denied(target: str) -> None:
    permissive = policy(
        connected_cidrs=("192.168.1.0/24",),
        allowed_cidrs=("0.0.0.0/0", "::/0"),
        allow_public=True,
    )
    with pytest.raises(ContractError, match="special-use"):
        normalize_network_targets([target], permissive)


def test_exact_loopback_is_allowed_but_loopback_expansion_is_denied() -> None:
    assert normalize_network_targets(["127.0.0.1"], policy()).target_count == 1
    assert normalize_network_targets(["::1"], policy()).target_count == 1
    with pytest.raises(ContractError, match="special-use"):
        normalize_network_targets(["127.0.0.0/24"], policy())


def test_public_target_requires_explicit_policy_and_action_approval() -> None:
    with pytest.raises(ContractError):
        normalize_network_targets(["8.8.8.8"], policy())
    enabled = policy(allowed_cidrs=("8.8.8.0/24",), allow_public=True)
    snapshot = normalize_network_targets(["8.8.8.8"], enabled)
    assert snapshot.approval_required is True


def test_explicit_private_target_stays_private_when_public_policy_is_enabled() -> None:
    enabled = policy(allowed_cidrs=("192.168.1.7/32",), allow_public=True)
    snapshot = normalize_network_targets(["192.168.1.7"], enabled)

    assert snapshot.approval_required is False
    assert snapshot.bindings[0].classification == "connected_private_ipv4"

    configured_only = policy(
        connected_cidrs=(),
        allowed_cidrs=("192.168.1.7/32",),
        allow_public=True,
    )
    configured_snapshot = normalize_network_targets(["192.168.1.7"], configured_only)
    assert configured_snapshot.approval_required is False
    assert configured_snapshot.bindings[0].classification == "operator_approved_private"


def test_hostname_is_resolved_to_pinned_ips_and_mixed_policy_answer_is_denied() -> None:
    snapshot = normalize_network_targets(
        ["router.example"],
        policy(),
        resolver=lambda _host: ("192.168.1.2", "192.168.1.3"),
    )
    assert snapshot.execution_targets == ("192.168.1.2", "192.168.1.3")
    with pytest.raises(ContractError):
        normalize_network_targets(
            ["router.example"],
            policy(),
            resolver=lambda _host: ("192.168.1.2", "8.8.8.8"),
        )


def test_command_shaped_and_overlapping_targets_are_rejected() -> None:
    with pytest.raises(ContractError, match="command-shaped"):
        normalize_network_targets(["192.168.1.1;rm"], policy())
    with pytest.raises(ContractError, match="overlapping"):
        normalize_network_targets(["192.168.1.0/24", "192.168.1.7"], policy())
    with pytest.raises(ContractError, match="overlapping"):
        normalize_network_targets(
            ["router.example", "192.168.1.2"],
            policy(),
            resolver=lambda _host: ("192.168.1.2",),
        )


def test_scope_and_policy_drift_are_bounded() -> None:
    with pytest.raises(ContractError, match="cap"):
        normalize_network_targets(["10.0.0.0/23"], policy(connected_cidrs=("10.0.0.0/23",)))
    snapshot = normalize_network_targets(["192.168.1.7"], policy())
    with pytest.raises(ContractError, match="changed"):
        assert_target_snapshot_current(snapshot, policy(allowed_cidrs=("192.168.1.0/24",)))
