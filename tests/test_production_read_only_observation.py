"""Focused proof for the pure production scheduled-work observer."""

from __future__ import annotations

import dataclasses
import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

import pytest

from friday.diagnostics.production_observation import (
    ProductionObservationError,
    collect_production_read_only_observation,
)
from friday.diagnostics.runtime_lease import ProcessLease
from friday.secondary_product_witness import secondary_product_process_epoch_sha256
from friday.storage.models import Mission, MissionStatus, MissionTask, TaskKind, new_id

_CHALLENGE = "a" * 64


def _open(storage) -> sqlite3.Connection:
    storage.execute("SELECT 1").fetchone()
    return storage.conn


@contextmanager
def _backend_lease(settings):
    with ProcessLease(settings.state_dir / "backend.lock", protocol="friday.backend.v1"):
        yield


def _collect(settings, storage):
    return collect_production_read_only_observation(
        settings,
        storage,
        challenge_sha256=_CHALLENGE,
    )


def _seed_scheduled_work(storage) -> tuple[str, str]:
    private_mission = "PRIVATE-MISSION-BODY-9cb8"
    private_notification = "PRIVATE-REMINDER-BODY-536a"
    storage.ensure_user("owner")
    mission = Mission(
        id=new_id("mis"),
        user_id="owner",
        goal=private_mission,
        status=MissionStatus.READY,
        created_by="owner",
    )
    storage.create_mission(mission)
    task = MissionTask(
        id=new_id("mst"),
        mission_id=mission.id,
        user_id="owner",
        seq=1,
        kind=TaskKind.GATHER,
        instruction="PRIVATE-TASK-INSTRUCTION-e117",
    )
    storage.set_mission_plan(
        mission.id,
        "owner",
        [task],
        plan_summary="PRIVATE-PLAN-SUMMARY-8f0e",
        status=MissionStatus.READY,
    )
    assert storage.enqueue_notification(
        "owner",
        "5001",
        private_notification,
        kind="reminder",
        dedup_key="reminder:opaque:2026-09-01T10:00:00Z",
    )
    assert storage.silence_reminder(
        "owner",
        "reminder:opaque:dismissed",
        chat_id="5001",
    )
    storage.kv_set(
        "workers:health:mission_runner",
        json.dumps(
            {"status": "ok", "error": "PRIVATE-WORKER-ERROR-2a8f"},
            sort_keys=True,
        ),
    )
    storage.kv_set(
        "workers:health:reminders_scan",
        json.dumps({"status": "running", "detail": "PRIVATE-WORKER-DETAIL-e4ab"}, sort_keys=True),
    )
    # An unrelated worker key must not define or widen this journey's output.
    storage.kv_set(
        "workers:health:scheduled_backup",
        json.dumps({"status": "error", "error": "PRIVATE-UNRELATED-STATE-cf91"}, sort_keys=True),
    )
    return private_mission, private_notification


def _fingerprint(path: Path) -> tuple[object, ...]:
    if not path.exists() and not path.is_symlink():
        return ("absent",)
    status = path.lstat()
    return (
        "present",
        status.st_dev,
        status.st_ino,
        status.st_mode,
        status.st_size,
        status.st_mtime_ns,
        path.read_bytes(),
    )


def test_observation_is_immutable_body_free_and_canonical(settings, storage) -> None:
    private_mission, private_notification = _seed_scheduled_work(storage)

    with _backend_lease(settings):
        first = _collect(settings, storage)
        second = _collect(settings, storage)

    assert first == second
    assert first.canonical_bytes() == second.canonical_bytes()
    assert first.canonical_sha256() == second.canonical_sha256()
    assert json.loads(first.canonical_bytes()) == first.to_payload()
    assert first.scheduled_work.missions.ready == 1
    assert first.scheduled_work.mission_tasks.pending == 1
    assert first.scheduled_work.reminders.pending == 1
    assert first.scheduled_work.reminders.dismissed == 1
    assert first.scheduled_work.workers.present == 2
    assert first.scheduled_work.workers.missing == 0
    assert first.scheduled_work.workers.ok == 1
    assert first.scheduled_work.workers.running == 1
    assert first.scheduled_work.workers.error == 0
    assert first.backend_process_epoch_sha256 == secondary_product_process_epoch_sha256(os.getpid())

    rendered = repr(first) + first.canonical_bytes().decode("ascii")
    for forbidden in (
        private_mission,
        private_notification,
        "PRIVATE-TASK-INSTRUCTION",
        "PRIVATE-PLAN-SUMMARY",
        "PRIVATE-WORKER-ERROR",
        "PRIVATE-WORKER-DETAIL",
        "PRIVATE-UNRELATED-STATE",
        "mission_runner",
        "reminders_scan",
        "scheduled_backup",
        "release_binding",
        '"pid"',
        str(settings.database_path),
        str(settings.state_dir),
    ):
        assert forbidden not in rendered

    with pytest.raises(dataclasses.FrozenInstanceError):
        first.schema_version = 49  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        first.scheduled_work.missions.ready = 2  # type: ignore[misc]


@pytest.mark.parametrize(
    "challenge",
    (
        "A" * 64,
        "a" * 63,
        "a" * 65,
        "g" * 64,
        "0" * 64,
        None,
    ),
)
def test_malformed_external_bindings_fail_before_observation(
    settings,
    storage,
    challenge,
) -> None:
    with pytest.raises(ProductionObservationError, match="exact lowercase SHA-256"):
        collect_production_read_only_observation(
            settings,
            storage,
            challenge_sha256=challenge,
        )


def test_collector_requires_an_open_connection_and_current_backend_owner(settings, storage) -> None:
    with pytest.raises(ProductionObservationError, match="already-open"):
        _collect(settings, storage)

    _open(storage)
    with pytest.raises(ProductionObservationError, match="own the backend lease"):
        _collect(settings, storage)


def test_collector_uses_one_connection_and_restores_every_read_only_boundary(
    settings,
    storage,
    monkeypatch,
) -> None:
    _seed_scheduled_work(storage)
    conn = storage.conn
    assert conn.execute("PRAGMA query_only").fetchone()[0] == 0
    before_changes = conn.total_changes
    queue = settings.state_dir / "telegram-inbox.sqlite3"
    queue.write_bytes(b"PRIVATE-BRIDGE-FILE-CANARY-6b1e")
    observed_paths = (
        settings.database_path,
        settings.database_path.with_name(f"{settings.database_path.name}-wal"),
        queue,
        queue.with_name(f"{queue.name}-wal"),
        queue.with_name(f"{queue.name}.lock"),
    )

    with _backend_lease(settings):
        before_files = {path: _fingerprint(path) for path in observed_paths}
        monkeypatch.setattr(
            sqlite3,
            "connect",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("second SQLite open")),
        )
        observed = _collect(settings, storage)
        after_files = {path: _fingerprint(path) for path in observed_paths}

    assert observed.backend_lease_owned is True
    assert storage.conn is conn
    assert conn.total_changes == before_changes
    assert conn.execute("PRAGMA query_only").fetchone()[0] == 0
    assert before_files == after_files
    # The collector must not poison the thread-local connection for later work.
    storage.kv_set("observer:test:post-write", "ok")
    assert storage.kv_get("observer:test:post-write") == "ok"


@pytest.mark.parametrize(
    "drift_sql",
    (
        "ALTER TABLE runtime_kv ADD COLUMN forged TEXT GENERATED ALWAYS AS ('x') VIRTUAL",
        "DROP INDEX uq_outbound_dedup",
        "UPDATE schema_meta SET value='49' WHERE key='schema_version'",
    ),
)
def test_schema_marker_hidden_columns_and_indexes_fail_closed_and_restore_query_only(
    settings,
    storage,
    drift_sql,
) -> None:
    _open(storage)
    with storage.transaction() as conn:
        conn.execute(drift_sql)

    with _backend_lease(settings), pytest.raises(ProductionObservationError):
        _collect(settings, storage)

    assert storage.conn.execute("PRAGMA query_only").fetchone()[0] == 0


@pytest.mark.parametrize(
    "drift_sql",
    (
        "CREATE TEMP TABLE missions(id TEXT)",
        """CREATE TRIGGER observer_mission_drift AFTER INSERT ON missions
             BEGIN SELECT 1; END""",
    ),
    ids=("protected-temp-shadow", "protected-trigger"),
)
def test_table_sql_trigger_and_temp_shadow_drift_fail_closed(
    settings,
    storage,
    drift_sql,
) -> None:
    conn = _open(storage)
    conn.execute(drift_sql)

    with _backend_lease(settings), pytest.raises(ProductionObservationError, match="drifted|shadowed"):
        _collect(settings, storage)

    assert conn.execute("PRAGMA query_only").fetchone()[0] == 0


def test_extra_unique_semantics_fail_closed(settings, storage) -> None:
    conn = _open(storage)
    conn.execute("CREATE UNIQUE INDEX observer_extra_mission_unique ON missions(title)")

    with (
        _backend_lease(settings),
        pytest.raises(ProductionObservationError, match="unique index surface"),
    ):
        _collect(settings, storage)


def test_legacy_migrated_mission_column_layout_is_an_exact_supported_schema(
    settings,
    storage,
) -> None:
    conn = _open(storage)
    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        conn.executescript(
            """
            DROP TABLE missions;
            CREATE TABLE missions (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL REFERENCES users(id),
                goal TEXT NOT NULL,
                title TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'ready'
                    CHECK(status IN ('proposed', 'ready', 'running', 'paused', 'blocked',
                                     'completed', 'failed', 'cancelled')),
                origin TEXT NOT NULL DEFAULT 'user'
                    CHECK(origin IN ('user', 'agent', 'worker')),
                plan_summary TEXT NOT NULL DEFAULT '',
                created_by TEXT NOT NULL DEFAULT '',
                error TEXT NOT NULL DEFAULT '',
                task_count INTEGER NOT NULL DEFAULT 0,
                done_count INTEGER NOT NULL DEFAULT 0,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                version INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                started_at TEXT,
                completed_at TEXT,
                budget_seconds INTEGER NOT NULL DEFAULT 0,
                budget_tool_calls INTEGER NOT NULL DEFAULT 0,
                budget_retries INTEGER NOT NULL DEFAULT 0,
                spent_seconds INTEGER NOT NULL DEFAULT 0,
                spent_tool_calls INTEGER NOT NULL DEFAULT 0,
                spent_retries INTEGER NOT NULL DEFAULT 0,
                deadline_at TEXT
            );
            CREATE INDEX idx_missions_user_status
                ON missions(user_id, status, created_at DESC);
            CREATE INDEX idx_missions_status ON missions(status, created_at);
            """
        )
    finally:
        conn.execute("PRAGMA foreign_keys=ON")

    with _backend_lease(settings):
        observed = _collect(settings, storage)

    assert observed.schema_version == 50
    assert observed.scheduled_work.missions.ready == 0


def test_existing_query_only_boundary_is_preserved(settings, storage) -> None:
    conn = _open(storage)
    conn.execute("PRAGMA query_only=ON")
    try:
        with _backend_lease(settings):
            observed = _collect(settings, storage)
        assert observed.database_integrity == "ok"
        assert conn.execute("PRAGMA query_only").fetchone()[0] == 1
    finally:
        conn.execute("PRAGMA query_only=OFF")


def test_foreign_key_drift_fails_closed_without_publishing_row_identity(settings, storage) -> None:
    conn = _open(storage)
    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        with storage.transaction() as transaction:
            transaction.execute(
                """INSERT INTO outbound_notifications(
                       id,user_id,chat_id,kind,dedup_key,body,status,attempts,created_at)
                   VALUES('PRIVATE-ORPHAN-ID-07cc','missing-owner','5001','reminder',
                          'reminder:orphan','PRIVATE-ORPHAN-BODY-5dc1','pending',0,'now')"""
            )
    finally:
        conn.execute("PRAGMA foreign_keys=ON")

    with (
        _backend_lease(settings),
        pytest.raises(
            ProductionObservationError,
            match="foreign key check failed",
        ) as failure,
    ):
        _collect(settings, storage)

    assert "PRIVATE-ORPHAN" not in str(failure.value)
    assert conn.execute("PRAGMA query_only").fetchone()[0] == 0


@pytest.mark.parametrize(
    "contradiction_sql",
    (
        "UPDATE mission_tasks SET side_effect=1 WHERE status='pending'",
        "UPDATE mission_tasks SET status='uncertain' WHERE status='pending'",
        "UPDATE mission_tasks SET status='running',started_at=NULL WHERE status='pending'",
        "UPDATE mission_tasks SET user_id='other' WHERE status='pending'",
        "UPDATE outbound_notifications SET status='invented' WHERE kind='reminder'",
    ),
)
def test_hard_durable_state_contradictions_are_rejected(
    settings,
    storage,
    contradiction_sql,
) -> None:
    _seed_scheduled_work(storage)
    storage.ensure_user("other")
    with storage.transaction() as conn:
        conn.execute(contradiction_sql)

    with _backend_lease(settings), pytest.raises(ProductionObservationError, match="contradiction"):
        _collect(settings, storage)


@pytest.mark.parametrize(
    "worker_value",
    (
        "not-json",
        '{"status":"ok","status":"error"}',
        '{"status":"invented"}',
    ),
)
def test_malformed_or_ambiguous_scheduled_worker_state_fails_closed(
    settings,
    storage,
    worker_value,
) -> None:
    _open(storage)
    storage.kv_set("workers:health:mission_runner", worker_value)

    with _backend_lease(settings), pytest.raises(ProductionObservationError, match="worker health"):
        _collect(settings, storage)


def test_delegate_routes_and_bridge_state_are_outside_the_pure_collector() -> None:
    import friday.diagnostics as diagnostics
    import friday.diagnostics.production_observation as observation

    assert not hasattr(diagnostics, "collect_production_read_only_observation")
    assert not hasattr(observation, "router")
    assert "fastapi" not in observation.__dict__
    assert "ProcessLease" not in observation.__dict__
