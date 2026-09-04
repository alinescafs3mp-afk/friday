import pytest

from friday.orchestration.coding_worker_network import (
    CODING_WORKER_NETWORK_ALLOWLIST,
    CodingWorkerNetworkReason,
    CodingWorkerNetworkState,
    build_coding_worker_network,
    default_coding_worker_network_policy,
)


def test_empty_facts_are_empty() -> None:
    result = build_coding_worker_network("network:1", "turn:1")

    assert result.network is CodingWorkerNetworkState.EMPTY
    assert result.dependency_or_research_steps == ()


def test_default_policy_is_explicitly_disabled() -> None:
    result = default_coding_worker_network_policy("network:1", "turn:1")

    assert result.network is CodingWorkerNetworkState.DISABLED
    assert result.reason is CodingWorkerNetworkReason.NETWORK_DISABLED
    assert result.allowlist == ()


def test_bounded_policy_requires_only_closed_dependency_or_research_steps() -> None:
    result = build_coding_worker_network(
        "network:1",
        "turn:1",
        {"policy": "bounded", "dependency_or_research_steps": ["dependency", "research"]},
    )

    assert result.network is CodingWorkerNetworkState.BOUNDED
    assert set(result.allowlist) == set(CODING_WORKER_NETWORK_ALLOWLIST)


@pytest.mark.parametrize(
    "facts",
    (
        {"host_network": True, "policy": "disabled"},
        {"unbounded": True, "policy": "disabled"},
        {"host_network": False},
        {"policy": "bounded"},
        {"policy": "bounded", "dependency_or_research_steps": []},
        {"policy": "bounded", "dependency_or_research_steps": ["shell"]},
        {"policy": "invented"},
    ),
)
def test_unsafe_missing_or_open_policy_blocks(facts: dict[str, object]) -> None:
    result = build_coding_worker_network("network:1", "turn:1", facts)

    assert result.network is CodingWorkerNetworkState.BLOCKED
    assert result.allowlist == ()


def test_disabled_policy_rejects_an_allowlist_and_facts_are_immutable() -> None:
    result = build_coding_worker_network(
        "network:1", "turn:1", {"policy": "disabled", "dependency_or_research_steps": ["research"]}
    )

    assert result.network is CodingWorkerNetworkState.BLOCKED
    with pytest.raises(AttributeError):
        result.network = CodingWorkerNetworkState.BOUNDED  # type: ignore[misc]
