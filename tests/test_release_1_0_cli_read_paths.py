"""Real argv dispatch for five CLI read paths; synthetic storage/provider boundaries.

The parser, dispatch and lease policy are real. Diagnostic/model report providers
are fixed fixtures; these tests grant no live-provider or all-table guarantee.
"""

from __future__ import annotations

import copy
import fcntl
import json
import os
import sys
from dataclasses import replace

import pytest

from friday import __version__, cli, config, diagnostics, model_check
from friday.diagnostics import runtime_lease
from friday.diagnostics.runtime_lease import ProcessLease
from friday.model_check import ModelReport, Probe
from friday.permissions import LEGACY_OWNER_USER_ID
from friday.storage.models import InboxItem, InboxStatus, RawObject, new_id

STAMP = "2026-01-02T03:04:05+00:00"
WORD = "azimuthomega"
STATE_TABLES = ("raw_objects", "inbox", "runtime_events")


def _same(actual, expected):
    assert type(actual) is type(expected)
    if isinstance(expected, dict):
        assert actual.keys() == expected.keys()
        for key in expected:
            _same(actual[key], expected[key])
    elif isinstance(expected, list):
        assert len(actual) == len(expected)
        for left, right in zip(actual, expected, strict=True):
            _same(left, right)
    else:
        assert actual == expected


def _snapshot(storage):
    return {
        name: [dict(row) for row in storage.execute(f"SELECT * FROM {name} ORDER BY rowid").fetchall()]
        for name in STATE_TABLES
    }


def _locked(path):
    if not path.exists():
        return False
    with path.open("r+b") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(stream, fcntl.LOCK_UN)
    return False


def _lease(settings, name):
    protocol = "friday.account-deletion.v1" if name == "account-deletion" else "friday.backend.v1"
    return ProcessLease(settings.state_dir / f"{name}.lock", protocol=protocol)


@pytest.fixture
def cli_context(settings, storage, monkeypatch):
    """Settings/log setup are isolated, but all command/lease selection stays real."""
    current = replace(settings, llm_enabled=True)
    config.ensure_runtime_dirs(current)
    calls = []
    original_ensure = config.ensure_runtime_dirs

    def ensure(observed):
        assert observed is current
        calls.append("ensure")
        return original_ensure(observed)

    monkeypatch.setattr(config, "load_settings", lambda: current)
    monkeypatch.setattr(config, "load_local_env_file", lambda: None)
    monkeypatch.setattr(config, "ensure_runtime_dirs", ensure)
    monkeypatch.setattr(cli, "configure_logging", lambda level: None)
    return current, storage, calls


def _run(monkeypatch, capsys, *arguments):
    capsys.readouterr()
    monkeypatch.setattr(sys, "argv", ["friday", *arguments])
    with pytest.raises(SystemExit) as exited:
        cli.main()
    assert type(exited.value.code) is int
    captured = capsys.readouterr()
    return exited.value.code, captured.out, captured.err


def _diagnostic_report(state):
    return {
        "state": state,
        "ok": state == "ready",
        "database": {"schema_version": 7, "integrity_check": "ok", "outbound_pending": 3},
        "backups": {"state": "missing", "verified": False},
        "workers": {"state": "stopped", "task_count": 0, "healthy": True},
        "backend_lease": {"state": "held", "pid": 101},
        "bridge_queue": {"state": "present", "pending": 2, "dead_letter": 1},
        "paths": {"model": {"usable_files_detected": False}},
        "configuration_issues": ["synthetic-issue"],
        "llm_endpoint": {"reachable": False},
        "actions": [
            {"severity": "warning", "title": "Check", "detail": "fixture", "command": "fixture-only"}
        ],
    }


@pytest.mark.parametrize("command", ["status", "doctor"])
@pytest.mark.parametrize("state,code", [("ready", 0), ("attention", 0), ("error", 1)])
def test_cli_diagnostics_json_uses_real_dispatch_leases_and_exact_exit(
    cli_context, monkeypatch, capsys, command, state, code
):
    settings, storage, ensures = cli_context
    report = _diagnostic_report(state)
    calls = []

    def collect(observed, *, check_llm_port, check_secrets):
        assert observed is settings
        calls.append((check_llm_port, check_secrets))
        assert _locked(settings.state_dir / "account-deletion.lock")
        assert _locked(settings.state_dir / "backend.lock")
        return copy.deepcopy(report)

    monkeypatch.setattr(diagnostics, "collect_diagnostics", collect)
    before = _snapshot(storage)
    args = [command, "--check-llm"] + (["--json"] if command == "status" else [])
    with _lease(settings, "backend"):
        actual_code, output, error = _run(monkeypatch, capsys, *args)
    assert actual_code == code and error == ""
    _same(json.loads(output), {"version": __version__, **report} if command == "status" else report)
    assert calls == [(True, True)]
    assert ensures == ["ensure"] * (1 if command == "status" else 2)
    assert not _locked(settings.state_dir / "account-deletion.lock")
    assert _snapshot(storage) == before


def test_cli_status_human_output_and_default_flags(cli_context, monkeypatch, capsys):
    settings, storage, _ = cli_context
    calls = []
    inspect_calls = []
    original_inspect = runtime_lease.inspect_process_lease
    bridge_path = settings.state_dir / "telegram-inbox.sqlite3.lock"

    def inspect(path, *, protocol):
        inspect_calls.append((path, protocol))
        return original_inspect(path, protocol=protocol)

    monkeypatch.setattr(runtime_lease, "inspect_process_lease", inspect)

    def collect(observed, *, check_llm_port, check_secrets):
        assert observed is settings
        calls.append((check_llm_port, check_secrets))
        return _diagnostic_report("ready")

    monkeypatch.setattr(diagnostics, "collect_diagnostics", collect)
    before = _snapshot(storage)
    with ProcessLease(bridge_path, protocol="friday.telegram-bridge.v1"):
        code, output, error = _run(monkeypatch, capsys, "status")
    assert code == 0 and error == ""
    assert calls == [(False, True)]
    assert inspect_calls == [(bridge_path, "friday.telegram-bridge.v1")]
    assert output.splitlines() == [
        f"Friday {__version__}",
        "State: ready",
        f"Home: {settings.home}",
        f"Database: {settings.database_path}",
        "Schema: 7",
        "Database integrity: ok",
        "Latest backup: missing",
        "Backend lease: held · pid 101",
        f"Bridge lease: active · pid {os.getpid()}",
        "Bridge queue: 2 pending · 1 dead-letter",
        "Outbound queue: 3 pending",
        "Workers: stopped · tasks 0 · healthy yes",
        f"Model directory: {settings.model_dir} (empty or missing)",
        "Configuration: OK",
        "  - synthetic-issue",
        "",
        "Recommended actions:",
        "  ~ Check: fixture",
        "      fixture-only",
    ]
    assert "LLM endpoint:" not in output
    assert _snapshot(storage) == before


def test_cli_doctor_default_flags_and_exact_ready_json(cli_context, monkeypatch, capsys):
    settings, storage, ensures = cli_context
    calls = []

    def collect(observed, *, check_llm_port, check_secrets):
        assert observed is settings and _locked(settings.state_dir / "account-deletion.lock")
        calls.append((check_llm_port, check_secrets))
        return _diagnostic_report("ready")

    monkeypatch.setattr(diagnostics, "collect_diagnostics", collect)
    before = _snapshot(storage)
    code, output, error = _run(monkeypatch, capsys, "doctor")
    assert code == 0 and error == ""
    _same(json.loads(output), _diagnostic_report("ready"))
    assert calls == [(False, True)] and ensures == ["ensure", "ensure"]
    assert _snapshot(storage) == before


@pytest.mark.parametrize("command", ["status", "doctor", "events", "search-source"])
def test_cli_account_deletion_lease_blocks_each_read_command_without_rows_changed(
    cli_context, monkeypatch, capsys, command
):
    settings, storage, _ = cli_context
    before = _snapshot(storage)
    arguments = [command] + ([WORD] if command == "search-source" else [])
    with _lease(settings, "account-deletion"):
        code, output, _error = _run(monkeypatch, capsys, *arguments)
    assert code == 2 and output == ""
    assert _snapshot(storage) == before


def test_cli_events_real_storage_has_exact_default_page_filter_and_empty_text(
    cli_context, monkeypatch, capsys
):
    settings, storage, _ = cli_context
    assert storage.count_events() == 0
    identifiers = []
    for index in range(52):
        identifiers.append(storage.record_event("cli-a" if index % 2 == 0 else "cli-b", {"seq": index}))
    with storage.transaction() as connection:
        connection.execute("UPDATE runtime_events SET created_at=?", (STAMP,))
    before = _snapshot(storage)
    original = type(storage).list_events
    original_count = type(storage).count_events
    calls = []
    count_calls = []

    def count_events(observed):
        assert _locked(settings.state_dir / "account-deletion.lock")
        count_calls.append("count")
        return original_count(observed)

    def list_events(observed, **kwargs):
        assert _locked(settings.state_dir / "account-deletion.lock")
        calls.append(kwargs.copy())
        return original(observed, **kwargs)

    monkeypatch.setattr(type(storage), "list_events", list_events)
    monkeypatch.setattr(type(storage), "count_events", count_events)
    expected = [
        {
            "id": str(identifiers[i]),
            "event_type": "cli-a" if i % 2 == 0 else "cli-b",
            "payload": {"seq": i},
            "created_at": STAMP,
        }
        for i in range(51, -1, -1)
    ]
    with _lease(settings, "backend"):
        code, output, error = _run(monkeypatch, capsys, "events", "--json")
        assert code == 0 and error == ""
        _same(json.loads(output), expected[:50])
        assert count_calls == ["count"] and _snapshot(storage) == before
        code, output, error = _run(monkeypatch, capsys, "events", "--type", "cli-a", "--limit", "2", "--json")
        assert code == 0 and error == ""
        _same(json.loads(output), [expected[1], expected[3]])
        assert count_calls == ["count"] * 2 and _snapshot(storage) == before
        code, output, error = _run(monkeypatch, capsys, "events", "--type", "absent")
        assert (code, output, error) == (0, "Событий типа absent нет.\n", "")
        assert count_calls == ["count"] * 3 and _snapshot(storage) == before
        code, output, error = _run(monkeypatch, capsys, "events")
        assert code == 0 and error == ""
        wanted = "Последние 50 из 52 записей:\n\n" + "".join(
            f"  2026-01-02 03:04:05  {('cli-a' if i % 2 == 0 else 'cli-b'):20} seq={i}\n"
            for i in range(51, 1, -1)
        )
        assert output == wanted and count_calls == ["count"] * 4
    assert calls == [
        {"event_type": None, "limit": 50},
        {"event_type": "cli-a", "limit": 2},
        {"event_type": "absent", "limit": 50},
        {"event_type": None, "limit": 50},
    ]
    assert _snapshot(storage) == before


def test_cli_events_empty_store_has_literal_human_and_json_results(cli_context, monkeypatch, capsys):
    _settings, storage, _ = cli_context
    before = _snapshot(storage)
    assert _run(monkeypatch, capsys, "events") == (0, "Событий пока нет.\n", "")
    code, output, error = _run(monkeypatch, capsys, "events", "--json")
    assert code == 0 and error == ""
    _same(json.loads(output), [])
    assert _snapshot(storage) == before


def _raw_fixture(storage, person, label, status, *, deleted=False):
    raw = RawObject(
        id=new_id("raw"),
        user_id=person,
        source="upload",
        source_ref=f"cli089:{label}",
        raw_content=f"{WORD} {label}",
        content_type="text",
        received_at=STAMP,
        created_at=STAMP,
        deleted_at=STAMP if deleted else None,
    )
    storage.store_raw_object(raw)
    storage.store_inbox_item(
        InboxItem(id=new_id("inbox"), user_id=person, raw_object_id=raw.id, status=status)
    )
    return str(raw.id)


def test_cli_search_source_preserves_verdict_tenant_and_private_projection(cli_context, monkeypatch, capsys):
    settings, storage, _ = cli_context
    storage.ensure_user(LEGACY_OWNER_USER_ID)
    storage.ensure_user("cli089-foreign")
    kept = _raw_fixture(storage, LEGACY_OWNER_USER_ID, "kept", InboxStatus.PENDING)
    _raw_fixture(storage, LEGACY_OWNER_USER_ID, "ignored", InboxStatus.IGNORED)
    _raw_fixture(storage, LEGACY_OWNER_USER_ID, "deleted", InboxStatus.PENDING, deleted=True)
    foreign = _raw_fixture(storage, "cli089-foreign", "foreign", InboxStatus.PENDING)
    before = _snapshot(storage)
    original = type(storage).search_raw_objects
    calls = []

    def search(observed, user_id, query, **kwargs):
        assert _locked(settings.state_dir / "account-deletion.lock")
        calls.append((user_id, query, kwargs.copy()))
        return original(observed, user_id, query, **kwargs)

    monkeypatch.setattr(type(storage), "search_raw_objects", search)

    def expected(raw_id, label):
        return {
            "query": WORD,
            "count": 1,
            "items": [
                {
                    "id": raw_id,
                    "source": "upload",
                    "source_ref": f"cli089:{label}",
                    "content_type": "text",
                    "received_at": STAMP,
                    "excerpt": f"{WORD} {label}",
                    "inbox_status": "pending",
                    "knowledge_object_id": None,
                }
            ],
        }

    with _lease(settings, "backend"):
        code, output, error = _run(monkeypatch, capsys, "search-source", WORD, "--json")
        assert code == 0 and error == ""
        _same(json.loads(output), expected(kept, "kept"))
        assert _snapshot(storage) == before
        code, output, error = _run(
            monkeypatch, capsys, "search-source", WORD, "--user", "cli089-foreign", "--limit", "1", "--json"
        )
        assert code == 0 and error == ""
        _same(json.loads(output), expected(foreign, "foreign"))
        assert _snapshot(storage) == before
        assert _run(monkeypatch, capsys, "search-source", WORD) == (
            0,
            f"[pending] upload: cli089:kept\n    …{WORD} kept…\n\nНайдено: 1\n",
            "",
        )
    assert calls == [
        (LEGACY_OWNER_USER_ID, WORD, {"limit": 20}),
        ("cli089-foreign", WORD, {"limit": 1}),
        (LEGACY_OWNER_USER_ID, WORD, {"limit": 20}),
    ]
    assert _snapshot(storage) == before


def test_cli_search_source_empty_result_keeps_literal_explanation(cli_context, monkeypatch, capsys):
    _settings, storage, _ = cli_context
    before = _snapshot(storage)
    expected = "Ничего не найдено в доступном исходном тексте.\nОтклонённый в Inbox материал сюда не входит: это вердикт, а не фильтр.\n"
    assert _run(monkeypatch, capsys, "search-source", WORD) == (0, expected, "")
    assert _snapshot(storage) == before


@pytest.mark.parametrize("ok,code", [(True, 0), (False, 1)])
def test_cli_model_check_json_forwards_timeout_and_needs_no_account_lease(
    cli_context, monkeypatch, capsys, ok, code
):
    settings, storage, ensures = cli_context
    before = _snapshot(storage)
    calls = []

    def check(observed, *, timeout):
        assert observed is settings
        calls.append(timeout)
        return ModelReport(
            "http://fixture.invalid/v1", "fixture-model", [Probe("chat", ok, "fixture-only", 0.125, 4)]
        )

    monkeypatch.setattr(model_check, "check_model", check)
    with _lease(settings, "account-deletion"), _lease(settings, "backend"):
        actual_code, output, error = _run(monkeypatch, capsys, "model-check", "--json", "--timeout", "1")
    assert actual_code == code and error == ""
    _same(
        json.loads(output),
        {
            "base_url": "http://fixture.invalid/v1",
            "model": "fixture-model",
            "ok": ok,
            "probes": [{"name": "chat", "ok": ok, "detail": "fixture-only", "seconds": 0.125, "tokens": 4}],
        },
    )
    assert calls == [1.0] and type(calls[0]) is float
    assert ensures == []
    assert _snapshot(storage) == before


def test_cli_model_check_disabled_human_mode_still_calls_provider_boundary(cli_context, monkeypatch, capsys):
    settings, storage, ensures = cli_context
    disabled = replace(settings, llm_enabled=False)
    monkeypatch.setattr(config, "load_settings", lambda: disabled)
    before = _snapshot(storage)
    calls = []

    def check(observed, *, timeout):
        assert observed is disabled
        calls.append(timeout)
        return ModelReport(
            "http://fixture.invalid/v1", "fixture-model", [Probe("chat", True, "fixture-only")]
        )

    monkeypatch.setattr(model_check, "check_model", check)
    code, output, error = _run(monkeypatch, capsys, "model-check")
    assert code == 0 and error == ""
    assert output.startswith(
        "FRIDAY_LLM_ENABLED=0 — проверяю эндпоинт всё равно, но Friday его не использует.\n\n"
    )
    assert "Эндпоинт: http://fixture.invalid/v1\nМодель:   fixture-model\n" in output
    assert output.endswith("\nГотово к работе.\n")
    assert calls == [60] and ensures == []
    assert _snapshot(storage) == before


@pytest.mark.parametrize(
    "arguments",
    [
        ("doctor", "--json"),
        ("search-source",),
        ("events", "--limit", "bad"),
        ("model-check", "--timeout", "bad"),
    ],
)
def test_cli_invalid_argv_exits_before_handler_and_state_access(cli_context, monkeypatch, capsys, arguments):
    _settings, storage, ensures = cli_context
    before = _snapshot(storage)
    code, output, error = _run(monkeypatch, capsys, *arguments)
    assert code == 2 and output == ""
    assert "usage:" in error and "error:" in error
    assert ensures == [] and _snapshot(storage) == before
