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


def _write_protected_set_queue(path, rows):
    """Create a disposable stopped queue with tempting forbidden body columns."""

    import os
    import sqlite3

    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            """CREATE TABLE updates (
                   update_id INTEGER PRIMARY KEY,
                   payload_json TEXT NOT NULL,
                   status TEXT NOT NULL,
                   attempts INTEGER NOT NULL,
                   last_error TEXT NOT NULL,
                   failed_at REAL,
                   created_at REAL NOT NULL,
                   last_attempt_at REAL NOT NULL,
                   backend_response_json TEXT,
                   ordering_key TEXT
               )"""
        )
        conn.executemany(
            """INSERT INTO updates (
                   update_id, payload_json, status, attempts, last_error,
                   failed_at, created_at, last_attempt_at,
                   backend_response_json, ordering_key
               ) VALUES (
                   :update_id, :payload_json, :status, :attempts, :last_error,
                   :failed_at, :created_at, :last_attempt_at,
                   :backend_response_json, :ordering_key
               )""",
            rows,
        )
        conn.commit()
    finally:
        conn.close()
    os.chmod(path, 0o600)


def _protected_set_rows():
    common = {
        "status": "dead_letter",
        "last_error": "FORBIDDEN_ERROR_SENTINEL",
        "backend_response_json": '{"secret":"FORBIDDEN_BACKEND_SENTINEL"}',
        "ordering_key": "FORBIDDEN_ORDERING_SENTINEL",
    }
    return [
        {
            **common,
            "update_id": 20,
            "payload_json": '{"secret":"FORBIDDEN_PAYLOAD_TWENTY"}',
            "attempts": 3,
            "failed_at": 1788231845.25,
            "created_at": 1788220798.5,
            "last_attempt_at": 1788231845.25,
        },
        {
            **common,
            "update_id": 10,
            "payload_json": '{"secret":"FORBIDDEN_PAYLOAD_TEN"}',
            "attempts": 1,
            "failed_at": None,
            "created_at": 1788091200.125,
            "last_attempt_at": 0.0,
        },
        {
            **common,
            "update_id": 30,
            "payload_json": '{"secret":"FORBIDDEN_PENDING_PAYLOAD"}',
            "status": "pending",
            "attempts": 0,
            "failed_at": None,
            "created_at": 1788235200.0,
            "last_attempt_at": 0.0,
        },
    ]


def _collect_protected_set(path):
    from friday.diagnostics import _bridge_queue_counts_only, _PinnedBridgeQueue

    pinned = _PinnedBridgeQueue(path)
    try:
        return _bridge_queue_counts_only(pinned)
    finally:
        pinned.close()


def test_protected_dead_letter_set_is_stable_exact_and_body_free(tmp_path):
    import json

    baseline_path = tmp_path / "baseline" / "telegram-inbox.sqlite3"
    reordered_path = tmp_path / "reordered" / "telegram-inbox.sqlite3"
    body_changed_path = tmp_path / "body-changed" / "telegram-inbox.sqlite3"
    empty_path = tmp_path / "empty" / "telegram-inbox.sqlite3"
    baseline_rows = _protected_set_rows()
    _write_protected_set_queue(baseline_path, baseline_rows)
    _write_protected_set_queue(reordered_path, list(reversed(baseline_rows)))
    body_changed = [dict(row) for row in baseline_rows]
    for row in body_changed:
        row["payload_json"] = '{"different":"FORBIDDEN_CHANGED_PAYLOAD"}'
        row["backend_response_json"] = '{"different":"FORBIDDEN_CHANGED_BACKEND"}'
        row["last_error"] = "FORBIDDEN_CHANGED_ERROR"
        row["ordering_key"] = "FORBIDDEN_CHANGED_ORDERING"
    _write_protected_set_queue(body_changed_path, body_changed)
    _write_protected_set_queue(
        empty_path,
        [row for row in baseline_rows if row["status"] == "pending"],
    )

    baseline = _collect_protected_set(baseline_path)
    reordered = _collect_protected_set(reordered_path)
    body_changed_projection = _collect_protected_set(body_changed_path)
    empty_projection = _collect_protected_set(empty_path)

    assert baseline == reordered == body_changed_projection
    assert baseline == {
        "state": "present",
        "pending": 1,
        "dead_letter": 2,
        "dead_letter_set_sha256": "661de8124d16d9520e8d9aca0c2f5ab4eaa1551806ddbea8f0788fba52d63616",
        "dead_letter_identities": [
            {
                "update_id": 10,
                "row_fingerprint": "43395d3d367ebb3ea7b0396bd8b25dcef23701bcabbaa913ce4b1101e9983712",
            },
            {
                "update_id": 20,
                "row_fingerprint": "907dbd41a63a865d938057649dc6e3658625a8030fc8efea31ed43b29e7eca34",
            },
        ],
    }
    serialized = json.dumps(baseline, sort_keys=True)
    assert "FORBIDDEN" not in serialized
    assert empty_projection == {
        "state": "present",
        "pending": 1,
        "dead_letter": 0,
        "dead_letter_set_sha256": "f0be97144a676e0c1e9b25c932b45a99d01242bab59a4a8edcf0417bcf49f521",
        "dead_letter_identities": [],
    }


def test_protected_dead_letter_collector_accepts_the_real_inbox_schema(tmp_path):
    import json
    import os

    from friday.telegram_bridge import _UpdateInbox

    path = tmp_path / "real-queue" / "telegram-inbox.sqlite3"
    path.parent.mkdir(mode=0o700)
    inbox = _UpdateInbox(str(path))
    try:
        inbox.store({"update_id": 71, "message": {"text": "REAL_SCHEMA_BODY_SENTINEL"}})
        inbox.store({"update_id": 72, "message": {"text": "pending"}})
        inbox.mark_dead_letter(71, "REAL_SCHEMA_ERROR_SENTINEL")
    finally:
        inbox.close()
    os.chmod(path.parent, 0o700)
    os.chmod(path, 0o600)

    projection = _collect_protected_set(path)

    assert projection["pending"] == 1
    assert projection["dead_letter"] == 1
    assert [item["update_id"] for item in projection["dead_letter_identities"]] == [71]
    assert len(projection["dead_letter_identities"][0]["row_fingerprint"]) == 64
    assert len(projection["dead_letter_set_sha256"]) == 64
    assert "SENTINEL" not in json.dumps(projection, sort_keys=True)


def test_protected_dead_letter_set_distinguishes_every_exact_set_change(tmp_path):
    baseline_rows = _protected_set_rows()
    variants = {
        "baseline": baseline_rows,
        "extra": [
            *baseline_rows,
            {
                **baseline_rows[0],
                "update_id": 40,
                "created_at": 1788307200.75,
            },
        ],
        "missing": [row for row in baseline_rows if row["update_id"] != 10],
        "replaced": [{**row, "update_id": 11} if row["update_id"] == 10 else row for row in baseline_rows],
        "mutated": [{**row, "attempts": 2} if row["update_id"] == 10 else row for row in baseline_rows],
    }
    projections = {}
    for name, rows in variants.items():
        path = tmp_path / name / "telegram-inbox.sqlite3"
        _write_protected_set_queue(path, rows)
        projections[name] = _collect_protected_set(path)

    digests = {item["dead_letter_set_sha256"] for item in projections.values()}
    assert len(digests) == len(projections)
    assert projections["extra"]["dead_letter"] == 3
    assert projections["missing"]["dead_letter"] == 1
    assert projections["replaced"]["dead_letter"] == projections["baseline"]["dead_letter"] == 2
    assert projections["mutated"]["dead_letter"] == projections["baseline"]["dead_letter"] == 2
    assert [item["update_id"] for item in projections["replaced"]["dead_letter_identities"]] == [
        11,
        20,
    ]
    baseline_ten = projections["baseline"]["dead_letter_identities"][0]
    mutated_ten = projections["mutated"]["dead_letter_identities"][0]
    assert baseline_ten["update_id"] == mutated_ten["update_id"] == 10
    assert baseline_ten["row_fingerprint"] != mutated_ten["row_fingerprint"]


def test_protected_dead_letter_collector_never_observes_forbidden_columns(tmp_path, monkeypatch):
    import sqlite3

    import friday.diagnostics as diagnostics

    path = tmp_path / "queue" / "telegram-inbox.sqlite3"
    _write_protected_set_queue(path, _protected_set_rows())
    real_connect = sqlite3.connect
    observed_update_columns: list[str] = []

    def audited_connect(*args, **kwargs):
        conn = real_connect(*args, **kwargs)

        def authorize(action, table, column, _database, _trigger):
            if action == sqlite3.SQLITE_READ and table == "updates" and column:
                observed_update_columns.append(str(column))
            return sqlite3.SQLITE_OK

        conn.set_authorizer(authorize)
        return conn

    monkeypatch.setattr(diagnostics.sqlite3, "connect", audited_connect)
    projection = _collect_protected_set(path)

    allowed = {
        "update_id",
        "status",
        "attempts",
        "failed_at",
        "created_at",
        "last_attempt_at",
    }
    forbidden = {"payload_json", "backend_response_json", "last_error", "ordering_key"}
    assert set(observed_update_columns) == allowed
    assert not forbidden & set(observed_update_columns)
    assert set(projection) == {
        "state",
        "pending",
        "dead_letter",
        "dead_letter_set_sha256",
        "dead_letter_identities",
    }


def test_protected_dead_letter_identity_material_is_bounded(tmp_path):
    import pytest

    path = tmp_path / "queue" / "telegram-inbox.sqlite3"
    template = _protected_set_rows()[0]
    rows = [{**template, "update_id": update_id} for update_id in range(4097)]
    _write_protected_set_queue(path, rows)

    with pytest.raises(RuntimeError, match="identity limit exceeded"):
        _collect_protected_set(path)


def test_protected_dead_letter_collector_rejects_malformed_identity_values(tmp_path):
    import pytest

    template = _protected_set_rows()[0]
    malformed = {
        "negative-update-id": {**template, "update_id": -1},
        "negative-attempts": {**template, "attempts": -1},
        "text-created-at": {**template, "created_at": "not-a-real"},
        "infinite-last-attempt": {**template, "last_attempt_at": float("inf")},
    }
    for name, row in malformed.items():
        path = tmp_path / name / "telegram-inbox.sqlite3"
        _write_protected_set_queue(path, [row])
        with pytest.raises(RuntimeError, match="identity is malformed"):
            _collect_protected_set(path)


def test_protected_dead_letter_collector_rejects_sidecar_and_descriptor_replacement(tmp_path):
    import os

    import pytest

    from friday.diagnostics import _PinnedBridgeQueue

    path = tmp_path / "queue" / "telegram-inbox.sqlite3"
    replacement = tmp_path / "queue" / "replacement.sqlite3"
    _write_protected_set_queue(path, _protected_set_rows())
    _write_protected_set_queue(replacement, _protected_set_rows())
    wal = path.with_name(f"{path.name}-wal")
    wal.write_bytes(b"not a real WAL; its presence alone must close the boundary")
    os.chmod(wal, 0o600)
    with pytest.raises(RuntimeError, match="checkpointed stopped database"):
        _PinnedBridgeQueue(path)
    wal.unlink()

    pinned = _PinnedBridgeQueue(path)
    try:
        os.replace(replacement, path)
        with pytest.raises(RuntimeError, match="observer queue"):
            pinned.revalidate()
    finally:
        pinned.close()


def test_guarded_protected_set_requires_and_preserves_the_exact_bridge_lease(settings, tmp_path):
    from dataclasses import replace

    import pytest

    from friday.diagnostics import collect_document_contour_guarded_bridge_queue_snapshot
    from friday.diagnostics.runtime_lease import ProcessLease, process_owns_lease

    state_dir = tmp_path / "state"
    local = replace(settings, state_dir=state_dir)
    queue = state_dir / "telegram-inbox.sqlite3"
    _write_protected_set_queue(queue, _protected_set_rows())
    lease_path = queue.with_name(f"{queue.name}.lock")
    boundary = ProcessLease(lease_path, protocol="friday.telegram-bridge.v1")

    with pytest.raises(RuntimeError, match="guard is not held"):
        collect_document_contour_guarded_bridge_queue_snapshot(local, boundary)

    boundary.acquire()
    try:
        snapshot = collect_document_contour_guarded_bridge_queue_snapshot(local, boundary)
        assert snapshot["schema"] == "friday.document-contour-guarded-bridge-queue.v2"
        assert snapshot["bridge_guard_held"] is True
        assert snapshot["inbound_pending"] == 1
        assert snapshot["dead_letter"] == 2
        assert snapshot["dead_letter_set_sha256"] == (
            "661de8124d16d9520e8d9aca0c2f5ab4eaa1551806ddbea8f0788fba52d63616"
        )
        assert [item["update_id"] for item in snapshot["dead_letter_identities"]] == [10, 20]
        assert boundary.acquired is True
        assert process_owns_lease(lease_path, protocol="friday.telegram-bridge.v1") is True
    finally:
        boundary.release()


def test_observer_protected_set_acquires_releases_and_never_inspects_an_active_bridge(
    settings,
    storage,
    tmp_path,
):
    from dataclasses import replace

    from friday.diagnostics import collect_document_contour_observer_snapshot
    from friday.diagnostics.runtime_lease import ProcessLease, process_owns_lease

    state_dir = tmp_path / "observer-state"
    local = replace(settings, state_dir=state_dir)
    queue = state_dir / "telegram-inbox.sqlite3"
    _write_protected_set_queue(queue, _protected_set_rows())
    backend_lease = ProcessLease(state_dir / "backend.lock", protocol="friday.backend.v1")
    bridge_lease_path = queue.with_name(f"{queue.name}.lock")
    backend_lease.acquire()
    try:
        stopped = collect_document_contour_observer_snapshot(local, storage)
        assert stopped["schema"] == "friday.document-contour-observer-snapshot.v2"
        assert stopped["backend_lease_owned"] is True
        assert stopped["physical_outbound_pending"] == 0
        assert stopped["bridge_queue_state"] == "present"
        assert stopped["bridge_lease_acquired_for_snapshot"] is True
        assert stopped["bridge_lease_released"] is True
        assert stopped["inbound_pending"] == 1
        assert stopped["dead_letter"] == 2
        assert stopped["dead_letter_set_sha256"] == (
            "661de8124d16d9520e8d9aca0c2f5ab4eaa1551806ddbea8f0788fba52d63616"
        )
        assert [item["update_id"] for item in stopped["dead_letter_identities"]] == [10, 20]
        assert process_owns_lease(bridge_lease_path, protocol="friday.telegram-bridge.v1") is False

        live_bridge = ProcessLease(bridge_lease_path, protocol="friday.telegram-bridge.v1")
        live_bridge.acquire()
        try:
            active = collect_document_contour_observer_snapshot(local, storage)
            assert active["bridge_queue_state"] == "active_uninspected"
            assert active["bridge_lease_acquired_for_snapshot"] is False
            assert active["bridge_lease_released"] is False
            assert active["inbound_pending"] is None
            assert active["dead_letter"] is None
            assert active["dead_letter_set_sha256"] is None
            assert active["dead_letter_identities"] is None
            assert live_bridge.acquired is True
        finally:
            live_bridge.release()
    finally:
        backend_lease.release()


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
