"""Trusted local operator mint/revoke argv with real isolated storage and leases.

No real credentials, server, network or authentication bypass are used. These
checks cover CLI output and selected state; HTTP authentication is a separate case.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import re
import sys
from datetime import UTC, datetime, timedelta

import pytest

from friday import cli, config
from friday.diagnostics.runtime_lease import ProcessLease

TABLES = ("users", "api_tokens", "audit_log")
TARGET = "cli089-token-target"
FOREIGN = "cli089-token-foreign"


def _rows(storage):
    return {
        table: [dict(row) for row in storage.execute(f"SELECT * FROM {table} ORDER BY id").fetchall()]
        for table in TABLES
    }


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


def _lease(settings, name):
    protocol = "friday.account-deletion.v1" if name == "account-deletion" else "friday.backend.v1"
    return ProcessLease(settings.state_dir / f"{name}.lock", protocol=protocol)


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


@pytest.fixture
def token_cli(settings, storage, monkeypatch):
    monkeypatch.setattr(config, "load_settings", lambda: settings)
    monkeypatch.setattr(config, "load_local_env_file", lambda: None)
    monkeypatch.setattr(cli, "configure_logging", lambda level: None)
    storage.ensure_user(FOREIGN, source="fixture", display_name="foreign", preset_key="moderator")
    storage.create_api_token(FOREIGN, hashlib.sha256(b"synthetic-foreign-token").hexdigest(), label="foreign")
    return settings, storage


def _run(monkeypatch, capsys, *arguments):
    capsys.readouterr()
    monkeypatch.setattr(sys, "argv", ["friday", *arguments])
    with pytest.raises(SystemExit) as stopped:
        cli.main()
    assert type(stopped.value.code) is int
    output = capsys.readouterr()
    return stopped.value.code, output.out, output.err


@pytest.mark.parametrize(
    "existing,preset,ttl,wanted_preset",
    [
        (False, None, None, "user"),
        (False, "moderator", "30m", "moderator"),
        (True, None, "1h", "admin"),
        (True, "guest", None, "guest"),
    ],
)
def test_cli_mint_pins_output_hash_expiry_target_preset_and_foreign_preservation(
    token_cli, monkeypatch, capsys, existing, preset, ttl, wanted_preset
):
    settings, storage = token_cli
    if existing:
        storage.ensure_user(TARGET, source="fixture", display_name="before", preset_key="admin")
        storage.create_api_token(
            TARGET, hashlib.sha256(b"synthetic-sibling-token").hexdigest(), label="sibling"
        )
    before = _rows(storage)
    original = type(storage).create_api_token
    calls = []

    def create(observed, person, token_hash, **kwargs):
        assert _locked(settings.state_dir / "account-deletion.lock")
        assert _locked(settings.state_dir / "backend.lock")
        calls.append((person, token_hash, kwargs.copy()))
        return original(observed, person, token_hash, **kwargs)

    monkeypatch.setattr(type(storage), "create_api_token", create)
    args = ["mint-token", "--user", TARGET, "--label", "CLI fixture"]
    if preset:
        args += ["--preset", preset]
    if ttl:
        args += ["--ttl", ttl]
    started = datetime.now(UTC).replace(microsecond=0)
    code, stdout, stderr = _run(monkeypatch, capsys, *args)
    ended = datetime.now(UTC).replace(microsecond=0)
    assert code == 0
    match = re.fullmatch(r"API-токен \(показывается один раз\): (jrc_[A-Za-z0-9_-]{43})\n", stderr)
    assert match
    secret = match.group(1)
    public = json.loads(stdout)
    assert re.fullmatch(r"tok_[0-9a-f]{16}", public["id"])
    after = _rows(storage)
    added = [row for row in after["api_tokens"] if row["id"] not in {r["id"] for r in before["api_tokens"]}]
    assert len(added) == 1
    row = added[0]
    assert row["id"] == public["id"]
    created = datetime.fromisoformat(row["created_at"])
    assert started <= created <= ended
    seconds = {None: None, "30m": 1800, "1h": 3600}[ttl]
    expiry = None if seconds is None else (created + timedelta(seconds=seconds)).isoformat(timespec="seconds")
    wanted_hash = hashlib.sha256(secret.encode()).hexdigest()
    _same(
        row,
        {
            "id": public["id"],
            "user_id": TARGET,
            "token_sha256": wanted_hash,
            "label": "CLI fixture",
            "created_by": "cli",
            "created_at": row["created_at"],
            "last_used_at": None,
            "revoked_at": None,
            "expires_at": expiry,
        },
    )
    _same(
        public,
        {
            "id": row["id"],
            "user_id": TARGET,
            "preset": wanted_preset,
            "label": "CLI fixture",
            "expires_at": expiry,
        },
    )
    assert calls == [
        (TARGET, wanted_hash, {"label": "CLI fixture", "created_by": "cli", "ttl_seconds": seconds})
    ]
    _same([r for r in after["api_tokens"] if r["id"] != row["id"]], before["api_tokens"])
    _same([r for r in after["users"] if r["id"] != TARGET], [r for r in before["users"] if r["id"] != TARGET])
    _same(after["audit_log"], before["audit_log"])
    user = next(r for r in after["users"] if r["id"] == TARGET)
    assert (
        user["preset_key"] == wanted_preset
        and user["source"] == "api-token"
        and user["display_name"] == TARGET
    )
    for key in ("updated_at", "last_seen_at"):
        assert started <= datetime.fromisoformat(user[key]) <= ended
    if existing:
        previous = next(r for r in before["users"] if r["id"] == TARGET)
        mutable = {"preset_key", "source", "display_name", "updated_at", "last_seen_at"}
        _same(
            {k: v for k, v in user.items() if k not in mutable},
            {k: v for k, v in previous.items() if k not in mutable},
        )
    else:
        assert len(after["users"]) == len(before["users"]) + 1
        assert user["status"] == "active" and user["metadata_json"] == "{}"
    assert secret not in stdout and secret not in json.dumps(after)
    assert not _locked(settings.state_dir / "account-deletion.lock") and not _locked(
        settings.state_dir / "backend.lock"
    )


@pytest.mark.parametrize(
    "arguments,needle",
    [
        (("--user", TARGET, "--preset", "bogus"), "Неизвестный preset: bogus."),
        (("--user", TARGET, "--ttl", "soon"), "Некорректный --ttl: 'soon'."),
        (("--user", "../bad-user"), "user_id must be 1-200 characters"),
    ],
)
def test_cli_mint_invalid_inputs_leave_selected_tables_and_no_secret(
    token_cli, monkeypatch, capsys, arguments, needle
):
    _settings, storage = token_cli
    before = _rows(storage)
    code, output, error = _run(monkeypatch, capsys, "mint-token", *arguments)
    assert code == 2 and output == "" and needle in error
    assert "API-токен" not in error
    _same(_rows(storage), before)


def test_cli_revoke_changes_only_target_timestamp_and_replay_missing_are_exact_refusals(
    token_cli, monkeypatch, capsys
):
    settings, storage = token_cli
    storage.ensure_user(TARGET)
    wanted = storage.create_api_token(
        TARGET, hashlib.sha256(b"synthetic-target-token").hexdigest(), label="target"
    )
    storage.create_api_token(TARGET, hashlib.sha256(b"synthetic-sibling-token").hexdigest(), label="sibling")
    before = _rows(storage)
    original = type(storage).revoke_api_token
    calls = []

    def revoke(observed, token_id, **kwargs):
        assert _locked(settings.state_dir / "account-deletion.lock") and _locked(
            settings.state_dir / "backend.lock"
        )
        calls.append((token_id, kwargs.copy()))
        return original(observed, token_id, **kwargs)

    monkeypatch.setattr(type(storage), "revoke_api_token", revoke)
    started = datetime.now(UTC).replace(microsecond=0)
    code, output, error = _run(monkeypatch, capsys, "revoke-token", wanted["id"])
    ended = datetime.now(UTC).replace(microsecond=0)
    assert code == 0 and error == ""
    _same(json.loads(output), {"status": "revoked", "id": wanted["id"]})
    after = _rows(storage)
    actual = next(r for r in after["api_tokens"] if r["id"] == wanted["id"])
    assert started <= datetime.fromisoformat(actual["revoked_at"]) <= ended
    expected = {
        **before,
        "api_tokens": [
            {**r, "revoked_at": actual["revoked_at"]} if r["id"] == wanted["id"] else r
            for r in before["api_tokens"]
        ],
    }
    _same(after, expected)
    for token_id in (wanted["id"], "tok_missing_fixture"):
        assert _run(monkeypatch, capsys, "revoke-token", token_id) == (
            2,
            "",
            "Токен не найден или уже отозван.\n",
        )
        _same(_rows(storage), after)
    assert calls == [(wanted["id"], {}), (wanted["id"], {}), ("tok_missing_fixture", {})]


@pytest.mark.parametrize("command", ["mint-token", "revoke-token"])
@pytest.mark.parametrize("name", ["account-deletion", "backend"])
def test_cli_token_commands_refuse_both_held_leases_without_rows_changed(
    token_cli, monkeypatch, capsys, caplog, command, name
):
    settings, storage = token_cli
    storage.ensure_user(TARGET)
    target = storage.create_api_token(
        TARGET, hashlib.sha256(b"synthetic-held-lease-target").hexdigest(), label="held-lease-target"
    )
    assert target["revoked_at"] is None
    revoke_calls = []
    original_revoke = type(storage).revoke_api_token

    def revoke(observed, token_id, **kwargs):
        revoke_calls.append((token_id, kwargs.copy()))
        return original_revoke(observed, token_id, **kwargs)

    monkeypatch.setattr(type(storage), "revoke_api_token", revoke)
    before = _rows(storage)
    args = [command] + (["--user", TARGET] if command == "mint-token" else [target["id"]])
    caplog.clear()
    with _lease(settings, name):
        code, output, error = _run(monkeypatch, capsys, *args)
    assert code == 2 and output == "" and "API-токен" not in error
    assert error != "Токен не найден или уже отозван.\n"
    assert [
        (record.levelname, record.getMessage()) for record in caplog.records if record.name == "friday.cli"
    ] == [("ERROR", "CLI command failed (RuntimeLeaseError)")]
    assert revoke_calls == []
    _same(_rows(storage), before)


@pytest.mark.parametrize("command", ["mint-token", "revoke-token"])
def test_cli_token_missing_required_argument_exits_before_storage(token_cli, monkeypatch, capsys, command):
    _settings, storage = token_cli
    before = _rows(storage)
    code, output, error = _run(monkeypatch, capsys, command)
    assert code == 2 and output == "" and "usage:" in error and "required" in error
    _same(_rows(storage), before)
