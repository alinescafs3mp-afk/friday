"""Real init/install-services argv and private artifacts; no daemon activation."""

from __future__ import annotations

import configparser
import os
import re
import subprocess
import sys

import pytest

from friday import cli, config
from friday.diagnostics.runtime_lease import ProcessLease
from friday.storage import FridayStorage as Storage


@pytest.fixture
def setup_context(settings, tmp_path, monkeypatch):
    config.ensure_runtime_dirs(settings)
    working = tmp_path / "working"
    working.mkdir()
    user_home = tmp_path / "operator"
    user_home.mkdir()
    monkeypatch.chdir(working)
    monkeypatch.setenv("HOME", str(user_home))
    monkeypatch.setenv("FRIDAY_ENV_FILE", "")
    monkeypatch.setattr(config, "load_settings", lambda: settings)
    monkeypatch.setattr(config, "load_local_env_file", lambda: None)
    monkeypatch.setattr(cli, "configure_logging", lambda _level: None)

    def forbidden_storage(*_args, **_kwargs):
        pytest.fail("setup command opened account storage")

    monkeypatch.setattr(Storage, "__init__", forbidden_storage)

    def forbidden_activation(*_args, **_kwargs):
        pytest.fail("setup command attempted an external process")

    for name in ("Popen", "run", "check_call", "check_output"):
        monkeypatch.setattr(subprocess, name, forbidden_activation)
    monkeypatch.setattr(os, "system", forbidden_activation)
    return settings, working, user_home


def _run(monkeypatch, capsys, *arguments):
    capsys.readouterr()
    monkeypatch.setattr(sys, "argv", ["friday", *arguments])
    with pytest.raises(SystemExit) as exited:
        cli.main()
    assert type(exited.value.code) is int
    captured = capsys.readouterr()
    return exited.value.code, captured.out, captured.err


def _held(settings, name):
    protocol = "friday.account-deletion.v1" if name == "account-deletion" else "friday.backend.v1"
    return ProcessLease(settings.state_dir / f"{name}.lock", protocol=protocol)


def _env_values(path):
    return dict(
        line.split("=", 1) for line in path.read_text().splitlines() if line and not line.startswith("#")
    )


@pytest.mark.parametrize("explicit", [False, True])
def test_cli_init_creates_private_configuration_without_account_access(
    setup_context, monkeypatch, capsys, explicit
):
    settings, working, _home = setup_context
    target = working / ("chosen.env" if explicit else ".env.local")
    arguments = ["--env-file", str(target), "init"] if explicit else ["init"]
    before = {p.name for p in working.iterdir()}
    with _held(settings, "account-deletion"), _held(settings, "backend"):
        code, output, error = _run(monkeypatch, capsys, *arguments)
    assert code == 0 and error == ""
    assert {p.name for p in working.iterdir()} == before | {target.name}
    assert target.stat().st_mode & 0o777 == 0o600
    values = _env_values(target)
    assert values["FRIDAY_HOME"] == str(settings.home)
    assert values["FRIDAY_MODEL_ROOT"] == str(settings.model_root)
    assert values["FRIDAY_LLM_API_KEY"] == values["FRIDAY_EMBEDDINGS_API_KEY"] == ""
    assert values["FRIDAY_EMBEDDINGS_ENABLED"] == "0"
    assert values["FRIDAY_SEMANTIC_SUPERVISOR_MODE"] == "off"
    assert values["FRIDAY_SEMANTIC_SUPERVISOR_PROMOTION_ENABLED"] == "0"
    credentials = [values["FRIDAY_API_TOKEN"], values["FRIDAY_TELEGRAM_BRIDGE_SECRET"]]
    assert len(set(credentials)) == 2
    assert all(re.fullmatch(r"[A-Za-z0-9_-]{64}", value) for value in credentials)
    assert all(value not in output + error for value in credentials)
    assert str(target) in output and "jericho up" in output


def test_cli_init_requires_force_and_rotates_only_the_requested_configuration(
    setup_context, monkeypatch, capsys
):
    _settings, working, _home = setup_context
    target = working / "chosen.env"
    other = working / "unrelated.env"
    original = b"FRIDAY_API_TOKEN=old-local-fixture\n"
    target.write_bytes(original)
    other.write_bytes(b"other-private-fixture\n")
    before = other.read_bytes()
    args = ["--env-file", str(target), "init"]
    code, output, error = _run(monkeypatch, capsys, *args)
    assert code == 2 and error == "" and "--force" in output
    assert target.read_bytes() == original and other.read_bytes() == before
    code, output, error = _run(monkeypatch, capsys, *args, "--force")
    assert code == 0 and error == ""
    assert target.read_bytes() != original and other.read_bytes() == before
    assert target.stat().st_mode & 0o777 == 0o600
    assert "old-local-fixture" not in output
    assert {p.name for p in working.iterdir()} == {target.name, other.name}
    assert b"old-local-fixture" not in target.read_bytes()
    values = _env_values(target)
    credentials = [values["FRIDAY_API_TOKEN"], values["FRIDAY_TELEGRAM_BRIDGE_SECRET"]]
    assert len(set(credentials)) == 2
    assert all(re.fullmatch(r"[A-Za-z0-9_-]{64}", value) for value in credentials)
    assert all(value not in output + error for value in credentials)


def test_cli_init_refuses_symlink_even_with_force_without_replacing_target(
    setup_context, monkeypatch, capsys
):
    _settings, working, _home = setup_context
    target = working / "foreign.env"
    target.write_bytes(b"foreign-private-fixture\n")
    link = working / "alias.env"
    link.symlink_to(target)
    before = target.read_bytes()
    code, _output, error = _run(monkeypatch, capsys, "--env-file", str(link), "init", "--force")
    assert code == 2 and "symlink" in error
    assert link.is_symlink() and target.read_bytes() == before
    assert {p.name for p in working.iterdir()} == {target.name, link.name}


@pytest.mark.parametrize("explicit", [False, True])
def test_cli_install_services_writes_exact_two_units_without_account_access_or_activation(
    setup_context, monkeypatch, capsys, explicit
):
    settings, working, home = setup_context
    target = working / "units" if explicit else home / ".config/systemd/user"
    env_file = working / "selected.env"
    env_file.write_text("FRIDAY_API_TOKEN=fixture-secret-not-for-units\n")
    args = ["--env-file", str(env_file), "install-services"]
    if explicit:
        args += ["--dir", str(target)]
    before = env_file.read_bytes()
    with _held(settings, "account-deletion"), _held(settings, "backend"):
        code, output, error = _run(monkeypatch, capsys, *args)
    assert code == 0 and error == ""
    names = {"jericho-backend.service", "jericho-bridge.service"}
    assert {p.name for p in target.iterdir()} == names
    for name, command, role, after in [
        ("jericho-backend.service", "server", "backend (API + Admin UI)", "network-online.target"),
        (
            "jericho-bridge.service",
            "telegram-bridge",
            "Telegram bridge",
            "network-online.target jericho-backend.service",
        ),
    ]:
        path = target / name
        parser = configparser.ConfigParser(interpolation=None)
        parser.optionxform = str
        parser.read_string(path.read_text())
        observed = {section: dict(parser[section]) for section in parser.sections()}
        expected = {
            "Unit": {"Description": f"Friday {role}", "After": after},
            "Service": {
                "Type": "simple",
                "ExecStart": f"{sys.executable} -m friday.cli --env-file {env_file} {command}",
                "Restart": "on-failure",
                "RestartSec": "5",
                "Environment": f"FRIDAY_HOME={settings.home}",
                "WorkingDirectory": str(settings.home),
            },
            "Install": {"WantedBy": "default.target"},
        }
        assert observed == expected
        assert "fixture-secret-not-for-units" not in path.read_text()
        assert str(path) in output
    assert env_file.read_bytes() == before
    assert "fixture-secret-not-for-units" not in output + error
    assert "daemon-reload" in output and "enable --now" in output
    assert not (home / ".config/systemd/user/default.target.wants").exists()
