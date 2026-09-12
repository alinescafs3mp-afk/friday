"""Real maintenance argv and lease boundaries; handler effects are separate cases.

Every handler is a distinct recorder. These tests prove parser dispatch, argument
values, both exclusive leases and refusal/cleanup, not maintenance data effects.
"""

from __future__ import annotations

import fcntl
import sys

import pytest

from friday import cli, config
from friday.diagnostics.runtime_lease import ProcessLease
from friday.storage import FridayStorage

# Literal command contracts, independent of the parser under test.
SPECS = [
    (
        "reindex-embeddings",
        "_reindex_embeddings",
        [],
        {"user": None, "yes": False},
        ["--user", "maintenance-owner", "--yes"],
        {"user": "maintenance-owner", "yes": True},
    ),
    (
        "backfill-document-dates",
        "_backfill_document_dates",
        [],
        {"user": None, "batch": 200, "limit": 0},
        ["--user", "maintenance-owner", "--batch", "7", "--limit", "11"],
        {"user": "maintenance-owner", "batch": 7, "limit": 11},
    ),
    (
        "backfill-entities",
        "_backfill_entities",
        ["--method", "explicit_person_patronymic"],
        {"method": "explicit_person_patronymic", "user": None, "batch": 200, "limit": 0, "apply": False},
        ["--user", "maintenance-owner", "--batch", "7", "--limit", "11", "--apply"],
        {
            "method": "explicit_person_patronymic",
            "user": "maintenance-owner",
            "batch": 7,
            "limit": 11,
            "apply": True,
        },
    ),
    (
        "prune-entities",
        "_prune_entities",
        [],
        {"user": None, "batch": 200, "limit": 0, "apply": False},
        ["--user", "maintenance-owner", "--batch", "7", "--limit", "11", "--apply"],
        {"user": "maintenance-owner", "batch": 7, "limit": 11, "apply": True},
    ),
    (
        "backfill-relations",
        "_backfill_relations",
        [],
        {"user": None, "batch": 200, "limit": 0, "apply": False},
        ["--user", "maintenance-owner", "--batch", "7", "--limit", "11", "--apply"],
        {"user": "maintenance-owner", "batch": 7, "limit": 11, "apply": True},
    ),
    (
        "extract-structure-relations",
        "_extract_structure_relations",
        [],
        {"user": None, "batch": 50, "limit": 0, "apply": False},
        ["--user", "maintenance-owner", "--batch", "7", "--limit", "11", "--apply"],
        {"user": "maintenance-owner", "batch": 7, "limit": 11, "apply": True},
    ),
    (
        "retag-documents",
        "_retag_documents",
        [],
        {
            "user": None,
            "batch": 100,
            "limit": 0,
            "arbiter": False,
            "learn_boilerplate": False,
            "rebuild_tags": False,
            "apply": False,
            "report": None,
        },
        [
            "--user",
            "maintenance-owner",
            "--batch",
            "7",
            "--limit",
            "11",
            "--arbiter",
            "--learn-boilerplate",
            "--rebuild-tags",
            "--apply",
            "--report",
            "chosen-report.jsonl",
        ],
        {
            "user": "maintenance-owner",
            "batch": 7,
            "limit": 11,
            "arbiter": True,
            "learn_boilerplate": True,
            "rebuild_tags": True,
            "apply": True,
            "report": "chosen-report.jsonl",
        },
    ),
    (
        "data-source",
        "_data_source",
        ["list", "--user", "maintenance-owner"],
        {
            "action": "list",
            "user": "maintenance-owner",
            "name": "",
            "kind": "sqlite",
            "dsn_env": "",
            "description": "",
            "query": "",
        },
        [
            "--name",
            "chosen",
            "--kind",
            "postgres",
            "--dsn-env",
            "TEST_MAINTENANCE_DSN",
            "--description",
            "fixture description",
            "--query",
            "SELECT 1",
        ],
        {
            "action": "list",
            "user": "maintenance-owner",
            "name": "chosen",
            "kind": "postgres",
            "dsn_env": "TEST_MAINTENANCE_DSN",
            "description": "fixture description",
            "query": "SELECT 1",
        },
    ),
    (
        "dismiss-series-conflicts",
        "_dismiss_series_conflicts",
        ["--user", "maintenance-owner"],
        {"user": "maintenance-owner", "apply": False},
        ["--apply"],
        {"user": "maintenance-owner", "apply": True},
    ),
    (
        "backfill-relation-dates",
        "_backfill_relation_dates",
        [],
        {"user": None, "apply": False},
        ["--user", "maintenance-owner", "--apply"],
        {"user": "maintenance-owner", "apply": True},
    ),
    (
        "review-relation-candidates",
        "_review_relation_candidates",
        ["--user", "maintenance-owner"],
        {"user": "maintenance-owner", "limit": 0, "apply": False, "votes": 2, "report": None},
        ["--limit", "11", "--apply", "--votes", "3", "--report", "chosen-votes.jsonl"],
        {"user": "maintenance-owner", "limit": 11, "apply": True, "votes": 3, "report": "chosen-votes.jsonl"},
    ),
    (
        "resolve-exact-duplicates",
        "_resolve_exact_duplicates",
        [],
        {"apply": False},
        ["--apply"],
        {"apply": True},
    ),
]


def _held(settings, name):
    protocol = "friday.account-deletion.v1" if name == "account-deletion" else "friday.backend.v1"
    return ProcessLease(settings.state_dir / f"{name}.lock", protocol=protocol)


def _locked(settings, name):
    path = settings.state_dir / f"{name}.lock"
    if not path.exists():
        return False
    with path.open("r+b") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(stream, fcntl.LOCK_UN)
    return False


@pytest.fixture
def dispatch_context(settings, monkeypatch, tmp_path):
    config.ensure_runtime_dirs(settings)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("FRIDAY_LOG_LEVEL", "INFO")
    monkeypatch.setattr(config, "load_settings", lambda: settings)
    monkeypatch.setattr(config, "load_local_env_file", lambda: None)
    monkeypatch.setattr(cli, "configure_logging", lambda _level: None)
    calls = []

    def forbidden_storage(*_args, **_kwargs):
        pytest.fail("dispatch-only probe opened account storage")

    monkeypatch.setattr(FridayStorage, "__init__", forbidden_storage)

    def recorder(identity):
        def handle(args):
            calls.append((identity, {k: v for k, v in vars(args).items() if k != "handler"}))
            assert _locked(settings, "account-deletion")
            assert _locked(settings, "backend")
            print(f"handler:{identity}")
            print(f"diagnostic:{identity}", file=sys.stderr)
            return 23

        return handle

    for _command, name, *_rest in SPECS:
        monkeypatch.setattr(cli, name, recorder(name))
    return settings, calls


def _run(monkeypatch, capsys, arguments):
    capsys.readouterr()
    monkeypatch.setattr(sys, "argv", ["friday", *arguments])
    with pytest.raises(SystemExit) as exited:
        cli.main()
    assert type(exited.value.code) is int
    captured = capsys.readouterr()
    return exited.value.code, captured.out, captured.err


@pytest.mark.parametrize("spec", SPECS, ids=[s[0] for s in SPECS])
@pytest.mark.parametrize("explicit", [False, True], ids=["defaults", "explicit"])
def test_maintenance_main_dispatches_exact_arguments_under_both_leases(
    dispatch_context, monkeypatch, capsys, spec, explicit
):
    settings, calls = dispatch_context
    command, identity, required, defaults, options, changed = spec
    code, output, error = _run(monkeypatch, capsys, [command, *required, *(options if explicit else [])])
    expected = {
        "command": command,
        "env_file": None,
        "log_level": "INFO",
        **(changed if explicit else defaults),
    }
    assert calls == [(identity, expected)]
    assert code == 23
    assert output == f"handler:{identity}\n" and error == f"diagnostic:{identity}\n"
    assert not _locked(settings, "account-deletion") and not _locked(settings, "backend")
    # Successful reacquisition proves both lease implementations released ownership.
    with _held(settings, "account-deletion"), _held(settings, "backend"):
        pass


@pytest.mark.parametrize("spec", SPECS, ids=[s[0] for s in SPECS])
@pytest.mark.parametrize("busy", ["account-deletion", "backend"])
def test_maintenance_main_refuses_each_active_lease_before_any_handler(
    dispatch_context, monkeypatch, capsys, spec, busy
):
    settings, calls = dispatch_context
    command, _identity, required, *_rest = spec
    other = "backend" if busy == "account-deletion" else "account-deletion"
    with _held(settings, busy):
        code, output, error = _run(monkeypatch, capsys, [command, *required])
        assert code == 2 and output == error == ""
        assert calls == []
        assert _locked(settings, busy) and not _locked(settings, other)
    with _held(settings, "account-deletion"), _held(settings, "backend"):
        pass


INVALID = [
    (["backfill-entities"], "--method"),
    (["data-source", "--user", "owner"], "action"),
    (["data-source", "list"], "--user"),
    (["data-source", "erase", "--user", "owner"], "invalid choice"),
    (["data-source", "add", "--user", "owner", "--kind", "shell"], "invalid choice"),
    (["dismiss-series-conflicts"], "--user"),
    (["review-relation-candidates"], "--user"),
    (["backfill-document-dates", "--batch", "NaN"], "invalid int value"),
    (["backfill-entities", "--method", "explicit_person_patronymic", "--batch", "NaN"], "invalid int value"),
    (["prune-entities", "--limit", "NaN"], "invalid int value"),
    (["backfill-relations", "--batch", "NaN"], "invalid int value"),
    (["extract-structure-relations", "--batch", "NaN"], "invalid int value"),
    (["retag-documents", "--batch", "NaN"], "invalid int value"),
    (["review-relation-candidates", "--user", "owner", "--votes", "NaN"], "invalid int value"),
]


@pytest.mark.parametrize("arguments,message", INVALID, ids=[f"invalid-{i}" for i in range(len(INVALID))])
def test_maintenance_main_rejects_invalid_arguments_before_lease_or_handler(
    dispatch_context, monkeypatch, capsys, arguments, message
):
    settings, calls = dispatch_context
    before = {p.name for p in settings.state_dir.iterdir()}
    code, output, error = _run(monkeypatch, capsys, arguments)
    assert code == 2 and output == "" and message in error
    assert calls == [] and {p.name for p in settings.state_dir.iterdir()} == before


@pytest.mark.parametrize(
    "failure", [ValueError, RuntimeError, FileNotFoundError], ids=lambda cls: cls.__name__
)
def test_maintenance_handler_failure_releases_leases_and_hides_exception_details(
    dispatch_context, monkeypatch, capsys, caplog, failure
):
    settings, calls = dispatch_context
    secret = "maintenance-error-private-fixture"

    def failing(_args):
        calls.append("failing")
        assert _locked(settings, "account-deletion") and _locked(settings, "backend")
        raise failure(secret)

    monkeypatch.setattr(cli, "_prune_entities", failing)
    with caplog.at_level("ERROR", logger="friday.cli"):
        code, output, error = _run(monkeypatch, capsys, ["prune-entities"])
    assert code == 2 and output == error == "" and calls == ["failing"]
    assert secret not in caplog.text + output + error
    assert [r.getMessage() for r in caplog.records if r.name == "friday.cli"] == [
        f"CLI command failed ({failure.__name__})"
    ]
    with _held(settings, "account-deletion"), _held(settings, "backend"):
        pass
