from __future__ import annotations

from dataclasses import replace

from friday.diagnostics import collect_diagnostics
from friday.storage import SCHEMA_VERSION, init_storage
from friday.telemetry import SystemTelemetry


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
        database_path=tmp_path / "home" / "data" / "friday.sqlite3",
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
    database_path = tmp_path / "absent" / "friday.sqlite3"
    local = replace(
        settings,
        database_path=database_path,
        backups_dir=tmp_path / "absent" / "backups",
    )

    result = collect_diagnostics(local)

    assert result["database"]["state"] == "not_initialized"
    assert result["database"]["schema_version"] is None
    assert not database_path.exists()


def test_active_backend_diagnostics_use_its_api_without_opening_the_main_wal(settings, monkeypatch):
    import friday.diagnostics as diagnostics

    lease = {"state": "active", "active": True, "healthy": True, "pid": 4242}
    remote = {
        "ok": True,
        "state": "ready",
        "database": {"ok": True, "state": "ready"},
        "workers": {"healthy": True, "tasks": {}},
        "backend_lease": lease,
        "bridge_queue": {"state": "active_uninspected", "healthy": True},
        "actions": [],
    }
    monkeypatch.setattr(diagnostics, "inspect_process_lease", lambda *_a, **_k: lease)
    monkeypatch.setattr(diagnostics, "_fetch_live_backend_diagnostics", lambda *_a, **_k: remote)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("active backend diagnostics must not map the main WAL")

    monkeypatch.setattr(diagnostics, "_database_status", forbidden)
    monkeypatch.setattr(diagnostics, "_worker_status", forbidden)
    monkeypatch.setattr(diagnostics, "_auth_failure_status", forbidden)

    assert diagnostics.collect_diagnostics(settings) is remote


def test_a_foreign_storage_argument_cannot_bypass_the_live_backend_boundary(settings, monkeypatch):
    import friday.diagnostics as diagnostics

    lease = {"state": "active", "active": True, "healthy": True, "pid": 4243}
    remote = {"ok": True, "state": "ready", "actions": []}
    monkeypatch.setattr(diagnostics, "inspect_process_lease", lambda *_a, **_k: lease)
    monkeypatch.setattr(diagnostics, "process_owns_lease", lambda *_a, **_k: False)
    monkeypatch.setattr(diagnostics, "_fetch_live_backend_diagnostics", lambda *_a, **_k: remote)

    class ForeignStorage:
        def diagnostics(self):
            raise AssertionError("foreign storage must not touch a live backend database")

    assert diagnostics.collect_diagnostics(settings, ForeignStorage()) is remote


def test_active_backend_api_failure_is_degraded_without_sqlite_fallback(settings, monkeypatch):
    import friday.diagnostics as diagnostics

    lease = {"state": "active", "active": True, "healthy": True, "pid": 4343}
    monkeypatch.setattr(diagnostics, "inspect_process_lease", lambda *_a, **_k: lease)
    monkeypatch.setattr(diagnostics, "_fetch_live_backend_diagnostics", lambda *_a, **_k: None)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("API failure must not fall back to a live SQLite mapping")

    monkeypatch.setattr(diagnostics, "_database_status", forbidden)
    monkeypatch.setattr(diagnostics, "_worker_status", forbidden)
    monkeypatch.setattr(diagnostics, "_auth_failure_status", forbidden)

    report = diagnostics.collect_diagnostics(settings)

    assert report["state"] == "degraded"
    assert report["database"]["state"] == "active_backend_uninspected"
    assert {item["code"] for item in report["actions"]} >= {"active_backend_diagnostics_unavailable"}


def test_backend_start_winning_after_probe_still_prevents_every_sqlite_open(settings, monkeypatch):
    """The lease boundary, not the earlier observation, owns the open decision."""

    import friday.diagnostics as diagnostics
    from friday.diagnostics.runtime_lease import RuntimeLeaseError

    inactive = {"state": "inactive", "active": False, "healthy": True, "pid": None}
    active = {"state": "active", "active": True, "healthy": True, "pid": 4444}
    inspections = iter((inactive, active))
    monkeypatch.setattr(diagnostics, "inspect_process_lease", lambda *_a, **_k: next(inspections))

    class LosingBoundary:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def acquire(self) -> None:
            raise RuntimeLeaseError("synthetic concurrent backend")

    monkeypatch.setattr(diagnostics, "ProcessLease", LosingBoundary)
    remote = {"ok": True, "state": "ready", "actions": []}
    monkeypatch.setattr(
        diagnostics,
        "_fetch_live_backend_diagnostics",
        lambda _settings, lease, **_kwargs: remote if lease is active else None,
    )

    def forbidden(*_args, **_kwargs):
        raise AssertionError("a lost lease race must not reach SQLite")

    monkeypatch.setattr(diagnostics, "_database_status", forbidden)
    monkeypatch.setattr(diagnostics, "_worker_status", forbidden)
    monkeypatch.setattr(diagnostics, "_auth_failure_status", forbidden)

    assert diagnostics.collect_diagnostics(settings) is remote


def test_live_diagnostics_bearer_target_must_be_proven_host_local(settings):
    import friday.diagnostics as diagnostics

    remote_named = replace(settings, api_host="diagnostics.invalid")
    assert (
        diagnostics._live_backend_diagnostics_url(  # noqa: SLF001
            remote_named,
            check_llm_port=False,
        )
        is None
    )
    assert diagnostics._live_backend_diagnostics_url(  # noqa: SLF001
        replace(settings, api_host="0.0.0.0"),
        check_llm_port=False,
    ).startswith("http://127.0.0.1:")
    secure = replace(
        settings,
        api_host="0.0.0.0",
        ssl_certfile="/public/server.crt",
        ssl_keyfile="/private/server.key",
    )
    assert diagnostics._live_backend_diagnostics_url(  # noqa: SLF001
        secure,
        check_llm_port=False,
    ).startswith("https://127.0.0.1:")
    assert diagnostics._live_backend_diagnostics_url(  # noqa: SLF001
        replace(secure, api_host="::"),
        check_llm_port=False,
    ).startswith("https://[::1]:")


def test_live_diagnostics_disables_proxy_redirects_and_rechecks_the_pid(settings, monkeypatch):
    import json
    import ssl
    import urllib.request

    import friday.diagnostics as diagnostics

    expected_pid = 4555
    payload = json.dumps(
        {
            "ok": True,
            "state": "ready",
            "database": {},
            "workers": {},
            "backend_lease": {"active": True, "state": "active", "pid": expected_pid},
            "bridge_queue": {},
            "actions": [],
        }
    ).encode()
    handlers: list[object] = []
    loaded_ca_files: list[str] = []

    class SSLContext:
        def load_verify_locations(self, *, cafile: str) -> None:
            loaded_ca_files.append(cafile)

    context = SSLContext()

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            pass

        def read(self, _limit: int) -> bytes:
            return payload

    class Opener:
        def open(self, request, *, timeout: float):
            assert timeout > 0
            assert request.full_url.startswith("https://127.0.0.1:")
            return Response()

    def build_opener(*items):
        handlers.extend(items)
        return Opener()

    monkeypatch.setattr(urllib.request, "build_opener", build_opener)
    monkeypatch.setattr(ssl, "create_default_context", lambda: context)
    # The serving process changed after its response.  A structurally valid old
    # response must not be accepted as evidence about the replacement backend.
    monkeypatch.setattr(
        diagnostics,
        "inspect_process_lease",
        lambda *_a, **_k: {"active": True, "state": "active", "pid": expected_pid + 1},
    )

    secure = replace(
        settings,
        api_host="0.0.0.0",
        ssl_certfile="/public/server.crt",
        ssl_keyfile="/private/server.key",
        backend_ca_file="/public/backend-ca.crt",
    )
    result = diagnostics._fetch_live_backend_diagnostics(  # noqa: SLF001
        secure,
        {"active": True, "state": "active", "pid": expected_pid},
        check_llm_port=False,
    )

    assert result is None
    proxy = next(item for item in handlers if isinstance(item, urllib.request.ProxyHandler))
    assert proxy.proxies == {}
    assert any(isinstance(item, diagnostics._NoLiveDiagnosticsRedirects) for item in handlers)  # noqa: SLF001
    assert any(isinstance(item, urllib.request.HTTPSHandler) for item in handlers)
    assert loaded_ca_files == ["/public/backend-ca.crt"]


def test_admin_overview_and_diagnostics_never_run_sqlite_work_on_the_event_loop():
    import inspect

    from friday.admin_api._overview import diagnostics, overview, settings_info

    for endpoint in (overview, settings_info, diagnostics):
        source = inspect.getsource(endpoint)
        assert "await run_blocking(" in source
        assert "_require(" not in source
        assert "storage." not in source
    assert "collect_diagnostics(" not in inspect.getsource(diagnostics)


def test_secret_scan_never_raw_opens_runtime_sqlite_artifacts(settings, monkeypatch):
    import friday.diagnostics as diagnostics
    from friday.secret_hygiene import Report

    captured: list[set] = []

    def fake_scan(*_args, **kwargs):
        captured.append({path.resolve() for path in kwargs.get("excluded", ())})
        return Report(exposures=[], loose_permissions=[], files_scanned=0, stopped_early=False)

    monkeypatch.setattr("friday.secret_hygiene.scan", fake_scan)
    diagnostics.collect_diagnostics(settings, check_secrets=True)

    assert len(captured) == 1
    database = settings.database_path
    queue = settings.state_dir / "telegram-inbox.sqlite3"
    expected = {
        path.resolve()
        for base in (database, queue)
        for path in (
            base,
            base.with_name(f"{base.name}-wal"),
            base.with_name(f"{base.name}-shm"),
            base.with_name(f"{base.name}-journal"),
        )
    }
    assert expected <= captured[0]


def test_diagnostics_turns_worker_failures_and_stalls_into_actions(settings, storage):
    import json
    from datetime import UTC, datetime, timedelta

    from friday.diagnostics.runtime_lease import ProcessLease

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

    lease = ProcessLease(local.state_dir / "backend.lock", protocol="friday.backend.v1")
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


def test_bridge_queue_dead_letters_are_observable_in_diagnostics(settings):
    from friday.diagnostics import _bridge_queue_status
    from friday.telegram_bridge import _UpdateInbox

    path = settings.state_dir / "telegram-inbox.sqlite3"
    path.parent.mkdir(parents=True, exist_ok=True)
    inbox = _UpdateInbox(str(path))
    try:
        inbox.store({"update_id": 1, "message": {"text": "pending"}})
        inbox.store({"update_id": 2, "message": {"text": "doomed"}})
        inbox.mark_dead_letter(2, "PermanentUpdateError: bad payload")
    finally:
        inbox.close()

    status = _bridge_queue_status(path)
    assert status["state"] == "present"
    assert status["pending"] == 1
    assert status["dead_letter"] == 1
    assert "bad payload" in status["last_dead_letter_error"]

    # The read-only view and a warning action reach collect_diagnostics.
    report = collect_diagnostics(settings)
    assert report["bridge_queue"]["dead_letter"] == 1
    assert any(a["code"] == "inspect_bridge_dead_letters" for a in report["actions"])


def test_bridge_queue_status_is_absent_when_no_inbox(settings):
    from friday.diagnostics import _bridge_queue_status

    status = _bridge_queue_status(settings.state_dir / "telegram-inbox.sqlite3")
    assert status["state"] == "absent"
    assert status["pending"] == 0 and status["dead_letter"] == 0 and status["healthy"] is True


def test_diagnostics_never_maps_the_queue_owned_by_a_live_bridge(settings, monkeypatch):
    import friday.diagnostics as diagnostics

    monkeypatch.setattr(
        diagnostics,
        "inspect_process_lease",
        lambda *_a, **_k: {"state": "active", "active": True, "healthy": True},
    )
    monkeypatch.setattr(
        diagnostics,
        "_bridge_queue_status",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("live bridge queue must not be opened")),
    )

    status = diagnostics._bridge_queue_status_without_live_open(  # noqa: SLF001
        settings.state_dir / "telegram-inbox.sqlite3"
    )

    assert status == {
        "state": "active_uninspected",
        "pending": None,
        "dead_letter": None,
        "healthy": True,
    }


def test_bridge_start_winning_after_probe_still_prevents_queue_open(settings, monkeypatch):
    import friday.diagnostics as diagnostics
    from friday.diagnostics.runtime_lease import RuntimeLeaseError

    inactive = {"state": "inactive", "active": False, "healthy": True}
    active = {"state": "active", "active": True, "healthy": True}
    inspections = iter((inactive, active))
    monkeypatch.setattr(diagnostics, "inspect_process_lease", lambda *_a, **_k: next(inspections))

    class LosingBoundary:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def acquire(self) -> None:
            raise RuntimeLeaseError("synthetic concurrent bridge")

    monkeypatch.setattr(diagnostics, "ProcessLease", LosingBoundary)
    monkeypatch.setattr(
        diagnostics,
        "_bridge_queue_status",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("lost race must not open queue")),
    )

    queue = settings.state_dir / "telegram-inbox.sqlite3"
    queue.parent.mkdir(parents=True, exist_ok=True)
    queue.write_bytes(b"synthetic stopped queue")
    status = diagnostics._bridge_queue_status_without_live_open(  # noqa: SLF001
        queue
    )

    assert status["state"] == "active_uninspected"
    assert status["pending"] is None and status["dead_letter"] is None


def test_llm_endpoint_status_unreachable_skips_http(settings):
    from friday.diagnostics import _llm_endpoint_status

    status = _llm_endpoint_status("http://127.0.0.1:1", "dispatcher", timeout=0.5)
    assert status["reachable"] is False
    assert status["model_served"] is None
    assert status["served_models"] == []


def test_diagnostics_flags_configured_model_not_served(settings, monkeypatch):
    import friday.diagnostics as diag

    tuned = replace(settings, llm_enabled=True)
    monkeypatch.setattr(
        diag,
        "_llm_endpoint_status",
        lambda *a, **k: {"reachable": True, "model_served": False, "served_models": ["other-model"]},
    )
    report = diag.collect_diagnostics(tuned, check_llm_port=True)
    codes = {a["code"] for a in report["actions"]}
    assert "llm_model_not_served" in codes  # reachable but wrong model name
    assert "start_llm_runtime" not in codes
    assert report["ok"] is False


def test_diagnostics_flags_llm_unreachable(settings, monkeypatch):
    import friday.diagnostics as diag

    tuned = replace(settings, llm_enabled=True)
    monkeypatch.setattr(
        diag, "_llm_endpoint_status", lambda *a, **k: {"reachable": False, "model_served": None}
    )
    report = diag.collect_diagnostics(tuned, check_llm_port=True)
    assert "start_llm_runtime" in {a["code"] for a in report["actions"]}
    assert report["ok"] is False


# --- auth-failure burst alerting ------------------------------------------


def _seed_auth_failures(storage, count, *, created_at=None):
    from friday.storage.models import AuditEntry, new_id, utc_now

    for index in range(count):
        entry = AuditEntry(
            id=new_id("audit"),
            user_id="anonymous",
            action="auth.failed",
            target_type="auth",
            target_id="invalid_credentials",
            after_json={"reason": "invalid_credentials", "status": 401},
            ip_address="10.0.0.9",
            request_id=f"req-{index}",
            created_at=created_at or utc_now(),
        )
        storage.log_audit(entry)


def test_count_recent_audit_respects_the_time_window(storage):
    from datetime import UTC, datetime, timedelta

    _seed_auth_failures(storage, 3)  # now
    old = (datetime.now(UTC) - timedelta(hours=2)).isoformat(timespec="seconds")
    _seed_auth_failures(storage, 5, created_at=old)  # 2h ago
    since = (datetime.now(UTC) - timedelta(hours=1)).isoformat(timespec="seconds")
    assert storage.count_recent_audit("auth.failed", since) == 3  # only the recent ones
    assert storage.count_recent_audit("nope", since) == 0
    # limit caps the scan for a threshold comparison (bounded cost on a bloated log).
    assert storage.count_recent_audit("auth.failed", since, limit=2) == 2


def test_auth_failure_burst_raises_a_warning_without_failing_ok(settings, storage):
    tuned = replace(settings, auth_failure_alert_threshold=3)
    _seed_auth_failures(storage, 4)  # >= threshold
    report = collect_diagnostics(tuned, storage)
    assert report["auth_failures"]["recent_failures"] == 4
    assert report["auth_failures"]["threshold"] == 3
    burst = [a for a in report["actions"] if a["code"] == "inspect_auth_failure_burst"]
    assert burst and burst[0]["severity"] == "warning"  # sentinel forwards warnings -> push
    # A burst is a warning, not a hard failure: it must not flip the ok flag.
    ok_without = collect_diagnostics(replace(settings, auth_failure_alert_threshold=0), storage)["ok"]
    assert report["ok"] == ok_without


def test_auth_failure_below_threshold_and_disabled_emit_no_action(settings, storage):
    _seed_auth_failures(storage, 2)
    below = collect_diagnostics(replace(settings, auth_failure_alert_threshold=5), storage)
    assert not any(a["code"] == "inspect_auth_failure_burst" for a in below["actions"])
    # threshold 0 disables the alert entirely even with many failures.
    _seed_auth_failures(storage, 20)
    disabled = collect_diagnostics(replace(settings, auth_failure_alert_threshold=0), storage)
    assert not any(a["code"] == "inspect_auth_failure_burst" for a in disabled["actions"])
    assert disabled["auth_failures"]["recent_failures"] == 22


def test_auth_failures_counted_read_only_without_open_storage(settings, tmp_path):
    local = replace(settings, database_path=tmp_path / "audit.sqlite3", auth_failure_alert_threshold=2)
    storage = init_storage(local)
    _seed_auth_failures(storage, 3)
    storage.close()
    # storage=None -> the read-only connection path still counts and alerts.
    report = collect_diagnostics(local, None)
    assert report["auth_failures"]["recent_failures"] == 3
    assert any(a["code"] == "inspect_auth_failure_burst" for a in report["actions"])


# --- Inbox backlog: material waiting long enough to be forgotten ----------


def _seed_pending(storage, count: int, *, age_days: int) -> None:
    from datetime import UTC, datetime, timedelta

    stamp = (datetime.now(UTC) - timedelta(days=age_days)).isoformat()
    storage.ensure_user("alice", source="upload")
    for index in range(count):
        # inbox.raw_object_id is a real foreign key; a fake id inserts nothing.
        storage.execute(
            "INSERT INTO raw_objects (id, user_id, source, source_ref, raw_content, "
            "content_type, content_hash, version, received_at, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                f"raw-{index}",
                "alice",
                "upload",
                f"sha256:{index:064d}",
                f"note {index}",
                "text/plain",
                f"{index:064d}",
                1,
                stamp,
                stamp,
            ),
        )
        storage.execute(
            "INSERT INTO inbox (id, user_id, raw_object_id, status, promotion_score, "
            "quality_score, created_at) VALUES (?,?,?,?,?,?,?)",
            (f"inbox-{index}", "alice", f"raw-{index}", "pending", 0.9, 0.9, stamp),
        )
    storage.conn.commit()


def test_backlog_alert_stays_quiet_right_after_an_import(settings, storage):
    """Thousands pending minutes after `jericho import` is exactly what should happen."""
    from friday.diagnostics import collect_diagnostics

    _seed_pending(storage, 500, age_days=0)
    report = collect_diagnostics(settings, storage)
    assert not [a for a in report["actions"] if a["code"] == "inbox_backlog"]


def test_backlog_alert_stays_quiet_for_a_handful_of_old_items(settings, storage):
    from friday.diagnostics import collect_diagnostics

    _seed_pending(storage, 5, age_days=400)
    report = collect_diagnostics(settings, storage)
    assert not [a for a in report["actions"] if a["code"] == "inbox_backlog"]


def test_backlog_alert_fires_once_material_has_been_ignored(settings, storage):
    """Pending means unsearchable: this is imported material the owner cannot reach."""
    from friday.diagnostics import collect_diagnostics

    _seed_pending(storage, 300, age_days=30)
    report = collect_diagnostics(settings, storage)
    alerts = [a for a in report["actions"] if a["code"] == "inbox_backlog"]
    assert len(alerts) == 1
    assert alerts[0]["severity"] == "warning"
    assert "300" in alerts[0]["detail"] and "30" in alerts[0]["detail"]


def test_backlog_alert_reaches_the_sentinel_severity_filter(settings, storage):
    """Sentinel only pushes error/warning; a signal below that would never be seen."""
    from friday.organs.sentinel import _ALERT_SEVERITIES

    _seed_pending(storage, 300, age_days=30)
    from friday.diagnostics import collect_diagnostics

    alert = [a for a in collect_diagnostics(settings, storage)["actions"] if a["code"] == "inbox_backlog"][0]
    assert alert["severity"] in _ALERT_SEVERITIES


def test_a_skipped_worker_is_a_valid_state_not_a_corrupt_one(settings, storage):
    """The orphan-thread guard's own signal decoded as corruption.

    The worker supervisor publishes `status="skipped"` when a previous run still
    has blocking work in flight — the guard added after two threads of one worker
    were reproduced on a single SQLite. The decoder's allowlist never learned the
    value, so the record became `invalid` with `state_errors: ["status"]` and the
    worker was reported degraded: the guard working correctly looked like broken
    state, and a genuinely corrupt record became indistinguishable from a healthy
    skip.
    """
    import json
    from datetime import UTC, datetime

    from friday.diagnostics import collect_diagnostics

    storage.kv_set(
        "workers:health:embeddings_index",
        json.dumps(
            {
                "name": "embeddings_index",
                "status": "skipped",
                "consecutive_failures": 0,
                "interval_sec": 120,
                "last_finished_at": datetime.now(UTC).isoformat(timespec="seconds"),
                "error_message": "previous run still has blocking work in flight",
            }
        ),
    )
    report = collect_diagnostics(replace(settings, workers_enabled=True), storage)
    tasks = report["workers"]["tasks"]
    record = tasks["embeddings_index"]
    assert record["status"] == "skipped"
    assert "state_errors" not in record
    assert "embeddings_index" not in report["workers"].get("degraded", [])


def test_a_mirror_that_stopped_working_reaches_the_operator(settings, storage, tmp_path):
    """The worker wrote its outcome every run, and nothing ever read the key.

    An offsite copy that stopped being made — an unplugged disk, a failing copy —
    was invisible in every surface: the report said the local backups were fine,
    which was true, and said nothing about the copy that exists precisely for the
    case where the local disk is gone.
    """
    import json

    from friday.diagnostics import collect_diagnostics

    mirrored = replace(settings, backup_mirror_dir=tmp_path / "mirror")

    storage.kv_set(
        "workers:last_backup_mirror",
        json.dumps({"enabled": True, "mirror_dir": str(tmp_path / "mirror"), "error": "mirror_dir_missing"}),
    )
    codes = {action["code"] for action in collect_diagnostics(mirrored, storage)["actions"]}
    assert "mirror_dir_missing" in codes

    storage.kv_set(
        "workers:last_backup_mirror",
        json.dumps({"enabled": True, "mirror_dir": str(tmp_path / "mirror"), "failed": 3}),
    )
    codes = {action["code"] for action in collect_diagnostics(mirrored, storage)["actions"]}
    assert "mirror_failed" in codes

    storage.kv_set(
        "workers:last_backup_mirror",
        json.dumps(
            {"enabled": True, "mirror_dir": str(tmp_path / "mirror"), "failed": 0, "same_device": True}
        ),
    )
    codes = {action["code"] for action in collect_diagnostics(mirrored, storage)["actions"]}
    assert "mirror_same_device" in codes

    # A healthy mirror raises nothing.
    storage.kv_set(
        "workers:last_backup_mirror",
        json.dumps({"enabled": True, "mirror_dir": str(tmp_path / "mirror"), "failed": 0, "copied": 1}),
    )
    codes = {action["code"] for action in collect_diagnostics(mirrored, storage)["actions"]}
    assert not {"mirror_dir_missing", "mirror_failed", "mirror_same_device", "mirror_stale"} & codes

    # Mirroring off means no mirror actions at all.
    codes = {action["code"] for action in collect_diagnostics(settings, storage)["actions"]}
    assert not [code for code in codes if code.startswith("mirror_")]
