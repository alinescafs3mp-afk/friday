from dataclasses import FrozenInstanceError

import pytest

from friday.orchestration.coding_worker_identity import (
    CODING_WORKER_IDENTITY_SCHEMA,
    CodingWorkerIdentityFactsV1,
    CodingWorkerIdentityReason,
    CodingWorkerIdentityState,
    CodingWorkerIdentityV1,
    build_coding_worker_identity,
    validate_coding_worker_identity,
)


def test_empty_facts_are_empty() -> None:
    result = build_coding_worker_identity("identity:1", "turn:1")

    assert result.identity is CodingWorkerIdentityState.EMPTY
    assert result.reason is CodingWorkerIdentityReason.NO_FACTS
    assert result.worker_id is None
    assert result.host_hostname is None


def test_three_opaque_worker_facts_are_identified_and_hostname_is_redacted() -> None:
    result = build_coding_worker_identity(
        "identity:1",
        "turn:1",
        {
            "worker_id": "worker:1",
            "operation_id": "operation:1",
            "project_id": "project:1",
            "hostname": "host.internal",
        },
    )

    assert result.identity is CodingWorkerIdentityState.IDENTIFIED
    assert result.worker_id == "worker:1"
    assert result.operation_id == "operation:1"
    assert result.project_id == "project:1"
    assert result.revision_selector is None
    assert result.host_hostname is None
    assert result.hostname is None
    with pytest.raises(FrozenInstanceError):
        result.worker_id = "other"  # type: ignore[misc]


def test_frozen_facts_and_optional_exact_revision_are_supported() -> None:
    result = build_coding_worker_identity(
        "identity:1",
        "turn:1",
        CodingWorkerIdentityFactsV1("worker:1", "operation:1", "project:1", "revision:1"),
    )

    assert result.identity is CodingWorkerIdentityState.IDENTIFIED
    assert result.revision_selector == "revision:1"


@pytest.mark.parametrize("selector", ("latest", "HEAD", "newest", "current", " Latest "))
def test_recency_selectors_fail_closed_without_worker_facts(selector: str) -> None:
    result = build_coding_worker_identity(
        "identity:1",
        "turn:1",
        {
            "worker_id": "worker:1",
            "operation_id": "operation:1",
            "project_id": "project:1",
            "revision_selector": selector,
            "hostname": "secret-host",
        },
    )

    assert result.identity is CodingWorkerIdentityState.BLOCKED
    assert result.reason is CodingWorkerIdentityReason.RECENCY_REVISION_SELECTOR
    assert result.worker_id is None
    assert result.operation_id is None
    assert result.project_id is None
    assert result.host_hostname is None


@pytest.mark.parametrize(
    "facts",
    (
        {"operation_id": "operation:1", "project_id": "project:1"},
        {"worker_id": "worker:1", "project_id": "project:1"},
        {"worker_id": "worker:1", "operation_id": "operation:1"},
        {"worker_id": "worker/project", "operation_id": "operation:1", "project_id": "project:1"},
        {"worker_id": 1, "operation_id": "operation:1", "project_id": "project:1"},
    ),
)
def test_missing_or_invalid_facts_are_blocked_and_redacted(facts: object) -> None:
    result = build_coding_worker_identity("identity:1", "turn:1", facts)  # type: ignore[arg-type]

    assert result.identity is CodingWorkerIdentityState.BLOCKED
    assert result.worker_id is None
    assert result.operation_id is None
    assert result.project_id is None
    assert result.revision_selector is None
    assert result.host_hostname is None


def test_mapping_round_trip_is_closed() -> None:
    result = build_coding_worker_identity(
        "identity:1",
        "turn:1",
        {"worker_id": "worker:1", "operation_id": "operation:1", "project_id": "project:1"},
    )
    encoded = result.to_mapping()

    assert encoded["schema"] == CODING_WORKER_IDENTITY_SCHEMA
    assert validate_coding_worker_identity(encoded) is True
    assert validate_coding_worker_identity({**encoded, "host_hostname": "leak"}) is False


def test_direct_result_rejects_exposed_hostname() -> None:
    with pytest.raises(ValueError):
        CodingWorkerIdentityV1(
            "identity:1",
            "turn:1",
            CodingWorkerIdentityState.BLOCKED,
            None,
            None,
            None,
            None,
            CodingWorkerIdentityReason.INVALID_FACTS,
            "host.internal",  # type: ignore[arg-type]
        )
