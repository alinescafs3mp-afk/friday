from __future__ import annotations

from dataclasses import replace

from jericho.diagnostics import collect_diagnostics
from jericho.storage import SCHEMA_VERSION, init_storage
from jericho.telemetry import SystemTelemetry


def test_telemetry_and_diagnostics_work_before_home_exists(settings, tmp_path):
    missing = tmp_path / "not-yet-created" / "jericho"
    snapshot = SystemTelemetry(missing).snapshot()
    assert snapshot["disk"]["path"] == str(missing)
    assert snapshot["disk"]["total_bytes"] is not None

    result = collect_diagnostics(settings)
    assert "runtime" in result and "paths" in result
    assert "schema_version" in result["database"]
    assert "backups" in result


def test_cli_style_diagnostics_reads_schema_and_backup_without_open_storage(settings, tmp_path):
    local = replace(
        settings,
        home=tmp_path / "home",
        state_dir=tmp_path / "home" / "data",
        database_path=tmp_path / "home" / "data" / "jericho.sqlite3",
        files_dir=tmp_path / "home" / "data" / "files",
        backups_dir=tmp_path / "home" / "data" / "backups",
        exports_dir=tmp_path / "home" / "data" / "exports",
        memory_vault_dir=tmp_path / "home" / "data" / "vault",
    )
    storage = init_storage(local)
    storage.ensure_user("doctor-user")
    backup = storage.create_backup(label="doctor")
    storage.close()

    result = collect_diagnostics(local)

    assert result["ok"] is True
    assert result["database"]["exists"] is True
    assert result["database"]["schema_version"] == SCHEMA_VERSION
    assert result["database"]["counts"]["users"] == 1
    assert result["backups"]["verified"] is True
    assert result["backups"]["latest"]["database"] == backup["database"]


def test_diagnostics_reports_uninitialized_database_without_creating_it(settings, tmp_path):
    database_path = tmp_path / "absent" / "jericho.sqlite3"
    local = replace(
        settings,
        database_path=database_path,
        backups_dir=tmp_path / "absent" / "backups",
    )

    result = collect_diagnostics(local)

    assert result["database"]["state"] == "not_initialized"
    assert result["database"]["schema_version"] is None
    assert not database_path.exists()


def test_diagnostics_turns_worker_failures_and_stalls_into_actions(settings, storage):
    import json
    from datetime import UTC, datetime, timedelta

    from jericho.diagnostics.runtime_lease import ProcessLease

    local = replace(settings, workers_enabled=True)
    old = (datetime.now(UTC) - timedelta(hours=2)).isoformat(timespec="seconds")
    storage.kv_set(
        "workers:health:failing_task",
        json.dumps(
            {
                "status": "error",
                "interval_sec": 60,
                "last_finished_at": old,
                "consecutive_failures": 3,
                "error_type": "RuntimeError",
                "error_message": "worker failed; inspect secure logs for details",
            }
        ),
    )
    storage.kv_set(
        "workers:health:stalled_task",
        json.dumps(
            {
                "status": "ok",
                "interval_sec": 60,
                "last_finished_at": old,
                "consecutive_failures": 0,
            }
        ),
    )

    lease = ProcessLease(local.state_dir / "backend.lock", protocol="jericho.backend.v1")
    lease.acquire()
    try:
        result = collect_diagnostics(local, storage)
    finally:
        lease.release()

    assert result["ok"] is False
    assert result["state"] == "degraded"
    assert result["backend_lease"]["state"] in {"active", "active_hint"}
    assert result["workers"]["degraded_tasks"] == ["failing_task"]
    assert result["workers"]["stale_tasks"] == ["failing_task", "stalled_task"]
    codes = {item["code"] for item in result["actions"]}
    assert {"inspect_failed_workers", "inspect_stale_workers"} <= codes


def test_diagnostics_worker_timestamps_are_not_false_alarm_when_backend_is_stopped(settings, storage):
    import json
    from datetime import UTC, datetime, timedelta

    local = replace(settings, workers_enabled=True)
    storage.kv_set(
        "workers:health:scheduled_backup",
        json.dumps(
            {
                "status": "ok",
                "interval_sec": 60,
                "last_finished_at": (datetime.now(UTC) - timedelta(days=2)).isoformat(timespec="seconds"),
                "consecutive_failures": 0,
            }
        ),
    )

    result = collect_diagnostics(local, storage)

    assert result["workers"]["stale_while_backend_stopped"] is True
    assert result["workers"]["healthy"] is True
    assert "inspect_stale_workers" not in {item["code"] for item in result["actions"]}


def test_diagnostics_handles_corrupt_worker_health_without_crashing(settings, storage):
    import json

    local = replace(settings, workers_enabled=True)
    storage.kv_set(
        "workers:health:corrupt_task",
        json.dumps(
            {
                "status": "impossible",
                "interval_sec": "NaN",
                "consecutive_failures": "many",
                "last_finished_at": "not-a-date",
            }
        ),
    )

    result = collect_diagnostics(local, storage)

    task = result["workers"]["tasks"]["corrupt_task"]
    assert task["status"] == "invalid"
    assert task["state_errors"] == ["consecutive_failures", "interval_sec", "status"]
    assert result["workers"]["degraded_tasks"] == ["corrupt_task"]
    assert result["state"] == "degraded"
