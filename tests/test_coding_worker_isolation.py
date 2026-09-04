from dataclasses import FrozenInstanceError

import pytest

from friday.orchestration.coding_worker_isolation import (
    CodingWorkerIsolationFactsV1,
    CodingWorkerIsolationReason,
    CodingWorkerIsolationState,
    build_coding_worker_isolation,
    validate_coding_worker_isolation,
)


def safe_facts() -> dict[str, bool]:
    return {
        "host_secrets_visible": False,
        "docker_socket_present": False,
        "production_database_reachable": False,
        "owner_ssh_keys_visible": False,
    }


def test_empty_facts_are_empty() -> None:
    result = build_coding_worker_isolation("isolation:1", "turn:1")

    assert result.isolation is CodingWorkerIsolationState.EMPTY
    assert result.reason is CodingWorkerIsolationReason.NO_FACTS
    assert result.host_secrets_visible is None


def test_all_false_facts_are_admitted_and_frozen() -> None:
    result = build_coding_worker_isolation(
        "isolation:1", "turn:1", CodingWorkerIsolationFactsV1(**safe_facts())
    )

    assert result.isolation is CodingWorkerIsolationState.ADMITTED
    assert result.reason is CodingWorkerIsolationReason.SAFE_FACTS
    assert result.host_secrets_visible is False
    with pytest.raises(FrozenInstanceError):
        result.isolation = CodingWorkerIsolationState.BLOCKED  # type: ignore[misc]


@pytest.mark.parametrize(
    ("unsafe", "reason"),
    (
        ("host_secrets_visible", CodingWorkerIsolationReason.HOST_SECRETS_VISIBLE),
        ("docker_socket_present", CodingWorkerIsolationReason.DOCKER_SOCKET_PRESENT),
        ("production_database_reachable", CodingWorkerIsolationReason.PRODUCTION_DATABASE_REACHABLE),
        ("owner_ssh_keys_visible", CodingWorkerIsolationReason.OWNER_SSH_KEYS_VISIBLE),
    ),
)
def test_any_true_fact_blocks_and_redacts_all_facts(unsafe: str, reason: CodingWorkerIsolationReason) -> None:
    facts = safe_facts()
    facts[unsafe] = True
    result = build_coding_worker_isolation("isolation:1", "turn:1", facts)

    assert result.isolation is CodingWorkerIsolationState.BLOCKED
    assert result.reason is reason
    assert result.to_mapping()["host_secrets_visible"] is None
    assert result.to_mapping()["docker_socket_present"] is None
    assert result.to_mapping()["production_database_reachable"] is None
    assert result.to_mapping()["owner_ssh_keys_visible"] is None


def test_missing_non_boolean_and_unknown_facts_fail_closed() -> None:
    missing = build_coding_worker_isolation("isolation:1", "turn:1", {"host_secrets_visible": False})
    invalid = build_coding_worker_isolation(
        "isolation:1", "turn:1", {**safe_facts(), "docker_socket_present": 0}
    )
    unknown = build_coding_worker_isolation("isolation:1", "turn:1", {**safe_facts(), "extra": False})

    assert missing.isolation is CodingWorkerIsolationState.BLOCKED
    assert invalid.isolation is CodingWorkerIsolationState.BLOCKED
    assert unknown.isolation is CodingWorkerIsolationState.BLOCKED
    assert missing.to_mapping()["owner_ssh_keys_visible"] is None


def test_mapping_round_trip_is_closed() -> None:
    result = build_coding_worker_isolation("isolation:1", "turn:1", safe_facts())

    assert validate_coding_worker_isolation(result.to_mapping()) is True
