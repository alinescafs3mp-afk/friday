from __future__ import annotations

import sqlite3

import pytest

from friday.host_control.job_schema import validate_host_control_job_schema
from friday.host_control.jobs import HostJobConflict, HostJobStore, HostJobTransitionError
from friday.permissions import LEGACY_OWNER_USER_ID
from friday.storage import SCHEMA_VERSION

_DIGEST_A = "a" * 64
_DIGEST_B = "b" * 64


def _create(store: HostJobStore, *, digest: str = _DIGEST_A, idem: str = "request-1"):
    return store.create_or_get(
        user_id=LEGACY_OWNER_USER_ID,
        actor_own_id=LEGACY_OWNER_USER_ID,
        conversation_id=None,
        source_message_id=None,
        host_agent_id="local-user-agent",
        capability_id="network.nmap.scan",
        adapter_id="network.nmap",
        adapter_version=1,
        action_id="discover",
        normalized_arguments={"targets": ["192.168.1.0/24"]},
        plan={"schema_version": 1, "plan_digest_input": digest},
        plan_digest=digest,
        risk_class="network_observe",
        authorization_basis="explicit_current_user_request",
        idempotency_key=idem,
        continuation={"kind": "none"},
    )


def test_schema_43_installs_exact_durable_host_action_projection(storage) -> None:
    assert SCHEMA_VERSION == 48
    assert storage.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()[0] == "48"
    with storage.transaction() as conn:
        validate_host_control_job_schema(conn)


def test_exact_host_job_retry_returns_existing_and_changed_retry_conflicts(storage) -> None:
    store = HostJobStore(storage)
    first, created = _create(store)
    replay, replay_created = _create(store)

    assert created is True
    assert replay_created is False
    assert replay["id"] == first["id"]
    with pytest.raises(HostJobConflict):
        _create(store, digest=_DIGEST_B)


def test_unknown_host_job_requires_reconciliation_and_never_replays(storage) -> None:
    store = HostJobStore(storage)
    job, _created = _create(store)
    admitted = store.transition(
        job["id"],
        user_id=LEGACY_OWNER_USER_ID,
        actor_own_id=LEGACY_OWNER_USER_ID,
        expected_status="planned",
        status="admitted",
        stage="agent_admission",
        outcome_code="admitted",
    )
    running = store.transition(
        job["id"],
        user_id=LEGACY_OWNER_USER_ID,
        actor_own_id=LEGACY_OWNER_USER_ID,
        expected_status="admitted",
        status="running",
        stage="process",
        outcome_code="process_started",
        systemd_unit="friday-host-job-example.service",
    )
    unknown = store.transition(
        job["id"],
        user_id=LEGACY_OWNER_USER_ID,
        actor_own_id=LEGACY_OWNER_USER_ID,
        expected_status="running",
        status="unknown",
        stage="receipt",
        outcome_code="agent_disconnected",
        error_code="transport_lost_after_admission",
    )

    assert admitted["status"] == "admitted"
    assert running["status"] == "running"
    assert unknown["status"] == "unknown"
    assert unknown["reconciliation_required"] is True
    assert [item["id"] for item in store.list_reconcilable(host_agent_id="local-user-agent")] == [job["id"]]
    with pytest.raises(HostJobTransitionError):
        store.transition(
            job["id"],
            user_id=LEGACY_OWNER_USER_ID,
            actor_own_id=LEGACY_OWNER_USER_ID,
            expected_status="unknown",
            status="running",
            stage="retry",
            outcome_code="forbidden_retry",
        )

    reconciling = store.transition(
        job["id"],
        user_id=LEGACY_OWNER_USER_ID,
        actor_own_id=LEGACY_OWNER_USER_ID,
        expected_status="unknown",
        status="reconciling",
        stage="reconcile",
        outcome_code="status_requested",
    )
    reconciled = store.transition(
        job["id"],
        user_id=LEGACY_OWNER_USER_ID,
        actor_own_id=LEGACY_OWNER_USER_ID,
        expected_status="reconciling",
        status="reconciled",
        stage="reconcile",
        outcome_code="process_absent",
    )
    assert reconciling["reconciliation_required"] is True
    assert reconciled["reconciliation_required"] is False
    assert len(store.events(job["id"], user_id=LEGACY_OWNER_USER_ID, actor_own_id=LEGACY_OWNER_USER_ID)) == 6


def test_host_plan_and_events_are_immutable(storage) -> None:
    store = HostJobStore(storage)
    job, _created = _create(store)

    with pytest.raises(sqlite3.IntegrityError):
        storage.execute(
            "UPDATE host_action_jobs SET plan_digest=? WHERE id=?",
            (_DIGEST_B, job["id"]),
        )
    with pytest.raises(sqlite3.IntegrityError):
        storage.execute("DELETE FROM host_action_events WHERE job_id=?", (job["id"],))


def test_host_job_is_person_scoped(storage) -> None:
    store = HostJobStore(storage)
    job, _created = _create(store)
    storage.ensure_user("another-person", preset_key="owner")

    assert (
        store.get(
            job["id"],
            user_id=LEGACY_OWNER_USER_ID,
            actor_own_id="another-person",
        )
        is None
    )
