from __future__ import annotations

import argparse
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from tools import quality_gate

CANONICAL_GATE_COMMAND = ".venv/bin/python tools/quality_gate.py"
ASSISTANT_GATE_GUIDANCE = (
    quality_gate.ROOT / "sol" / "SOL.md",
    quality_gate.ROOT / "grok" / "GROK.md",
    quality_gate.ROOT / "grok" / "NOTES.md",
)


def _args(**overrides: object) -> argparse.Namespace:
    values: dict[str, object] = {
        "phase": None,
        "workers": 12,
        "ui_workers": 9,
        "dry_run": False,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def test_static_gate_checks_the_current_package_and_high_bandit_only() -> None:
    commands = quality_gate.static_commands(python="python")
    command_by_name = {command.name: command.argv for command in commands}

    assert command_by_name["whitespace errors"] == ("git", "diff", "--check")
    assert command_by_name["ruff format"][-3:] == ("friday", "tests", "tools")
    assert command_by_name["mypy"][-1] == "friday"
    assert command_by_name["compileall"][-3:] == ("friday", "tests", "tools")
    assert any(argument.startswith("pycache_prefix=") for argument in command_by_name["compileall"])
    assert command_by_name["bandit (HIGH only)"] == (
        "python",
        "-m",
        "bandit",
        "-r",
        "friday",
        "-q",
        "--severity-level",
        "high",
    )
    assert command_by_name["admin JavaScript syntax"] == (
        "node",
        "--check",
        "friday/admin_ui/static/app.js",
    )
    assert all("jericho" not in argument for command in commands for argument in command.argv)
    assert all(command.environment is None for command in commands)


def test_run_command_passes_an_explicit_environment_to_the_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_run(argv: tuple[str, ...], **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured["argv"] = argv
        captured.update(kwargs)
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(quality_gate.subprocess, "run", fake_run)
    command = quality_gate.GateCommand("probe", ("python", "-V"), {"ONLY": "scratch"})

    assert quality_gate.run_command(command) == 0
    assert captured["env"] == {"ONLY": "scratch"}
    assert captured["cwd"] == quality_gate.ROOT
    assert captured["check"] is False


def test_pytest_phases_share_one_private_non_live_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for prefix in quality_gate._RUNTIME_ENV_PREFIXES:
        monkeypatch.setenv(prefix + "HOME", "/sentinel/live-home")
        monkeypatch.setenv(prefix + "ENV_FILE", "/sentinel/live.env")
        monkeypatch.setenv(prefix + "DATABASE_PATH", "/sentinel/live.sqlite3")
        monkeypatch.setenv(prefix + "DATABASE_MUST_EXIST", "1")
        for suffix in quality_gate._RUNTIME_PATH_SELECTOR_SUFFIXES:
            monkeypatch.setenv(prefix + suffix, f"/sentinel/{suffix.casefold()}")

    observed_homes: set[Path] = set()
    observed_commands: list[str] = []

    def runner(command: quality_gate.GateCommand) -> int:
        observed_commands.append(command.name)
        environment = command.environment
        assert environment is not None
        home = Path(environment["FRIDAY_HOME"])
        env_file = Path(environment["FRIDAY_ENV_FILE"])
        observed_homes.add(home)
        assert environment["JERICHO_HOME"] == str(home)
        assert environment["JERICHO_ENV_FILE"] == str(env_file)
        assert environment["FRIDAY_DATABASE_PATH"] == ""
        assert environment["JERICHO_DATABASE_PATH"] == ""
        assert environment["FRIDAY_DATABASE_MUST_EXIST"] == "0"
        assert environment["JERICHO_DATABASE_MUST_EXIST"] == "0"
        assert home.is_dir()
        assert env_file.is_file()
        assert env_file.is_relative_to(home)
        if os.name != "nt":
            assert stat.S_IMODE(home.stat().st_mode) == 0o700
            assert stat.S_IMODE(env_file.stat().st_mode) == 0o600
        for prefix in quality_gate._RUNTIME_ENV_PREFIXES:
            for suffix in quality_gate._RUNTIME_PATH_SELECTOR_SUFFIXES:
                assert prefix + suffix not in environment
        report_argument = next(
            (argument for argument in command.argv if argument.startswith("--junitxml=")),
            "",
        )
        if report_argument:
            Path(report_argument.partition("=")[2]).write_text(
                '<testsuite tests="1" skipped="0"/>',
                encoding="utf-8",
            )
        return 0

    result = quality_gate.execute(
        _args(phase=["tests", "ui"], workers=1, ui_workers=1),
        command_runner=runner,
        preflight=lambda: True,
    )

    assert result == 0
    assert observed_commands == ["non-UI tests", "UI tests"]
    assert len(observed_homes) == 1
    assert all(not home.exists() for home in observed_homes)


def test_eager_settings_import_derives_the_database_from_the_scratch_home(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FRIDAY_HOME", "/sentinel/live-home")
    monkeypatch.setenv("FRIDAY_STATE_DIR", "/sentinel/live-state")
    monkeypatch.setenv("FRIDAY_DATABASE_PATH", "/sentinel/live.sqlite3")
    monkeypatch.setenv("FRIDAY_DATABASE_MUST_EXIST", "1")
    monkeypatch.setenv("FRIDAY_ENV_FILE", "/sentinel/live.env")
    for prefix in quality_gate._RUNTIME_ENV_PREFIXES:
        monkeypatch.setenv(prefix + "SSL_CERTFILE", "/sentinel/live-server.crt")
        monkeypatch.setenv(prefix + "SSL_KEYFILE", "/sentinel/live-server.key")
        monkeypatch.setenv(prefix + "BACKEND_CA_FILE", "/sentinel/live-backend-ca.crt")

    with quality_gate._isolated_test_environment() as environment:
        probe = (
            "import os; "
            "from pathlib import Path; "
            "from friday.config import load_settings; "
            "settings = load_settings(); "
            "home = Path(os.environ['FRIDAY_HOME']).resolve(); "
            "assert settings.home == home; "
            "assert settings.state_dir.is_relative_to(home); "
            "assert settings.database_path.is_relative_to(home); "
            "assert settings.ssl_certfile == ''; "
            "assert settings.ssl_keyfile == ''; "
            "assert settings.backend_ca_file == ''"
        )
        subprocess.run(  # noqa: S603 - fixed local interpreter/import probe
            [sys.executable, "-c", probe],
            cwd=quality_gate.ROOT,
            env=environment,
            check=True,
        )


def test_assistant_instructions_delegate_to_the_canonical_gate() -> None:
    copied_internal_commands = (
        ".venv/bin/ruff ",
        ".venv/bin/mypy ",
        ".venv/bin/bandit ",
        ".venv/bin/python -m pytest",
        "node --check friday/",
    )

    for path in ASSISTANT_GATE_GUIDANCE:
        text = path.read_text(encoding="utf-8-sig")
        assert CANONICAL_GATE_COMMAND in text, (
            f"{path.relative_to(quality_gate.ROOT)} bypasses the canonical gate"
        )
        copied = [command for command in copied_internal_commands if command in text]
        assert not copied, (
            f"{path.relative_to(quality_gate.ROOT)} copies gate internals {copied}; "
            "call the canonical runner so package paths cannot drift"
        )


def test_non_ui_tests_exclude_all_eleven_browser_modules() -> None:
    command = quality_gate.non_ui_command(workers=12, python="python")

    # Число здесь стоит затем, чтобы браузерный модуль нельзя было завести молча
    # и потерять из общего прогона. Двенадцатый добавлен в 0.196.0 вместе со
    # второй осью времени и разведением кратных рёбер.
    assert len(quality_gate.UI_TEST_MODULES) == 12
    assert command.argv[5:8] == ("-n", "12", "--dist=load")
    assert {
        argument.removeprefix("--ignore=") for argument in command.argv if argument.startswith("--ignore=")
    } == set(quality_gate.UI_TEST_MODULES)


def test_ui_module_inventory_cannot_silently_drift() -> None:
    playwright_import_skip = "importorskip(" + '"playwright.sync_api")'
    discovered = {
        path.relative_to(quality_gate.ROOT).as_posix()
        for path in (quality_gate.ROOT / "tests").glob("test_*.py")
        if playwright_import_skip in path.read_text(encoding="utf-8")
    }

    assert discovered == set(quality_gate.UI_TEST_MODULES)


def test_ui_tests_use_one_loadscope_worker_per_module() -> None:
    command = quality_gate.ui_command(report_path="report.xml", workers=12, python="python")

    assert command.argv[6:9] == ("-n", "12", "--dist=loadscope")
    assert command.argv[-12:] == quality_gate.UI_TEST_MODULES
    assert "--junitxml=report.xml" in command.argv


def test_ui_tests_have_a_serial_fallback() -> None:
    command = quality_gate.ui_command(report_path="report.xml", workers=1, python="python")

    assert command.argv[6:8] == ("-n", "0")
    assert "--dist=loadscope" not in command.argv


def test_ui_junit_skips_are_counted(tmp_path: Path) -> None:
    report = tmp_path / "ui.xml"
    report.write_text(
        '<testsuites><testsuite tests="5" skipped="2"/><testsuite tests="3" skipped="1"/></testsuites>',
        encoding="utf-8",
    )

    assert quality_gate.junit_skip_count(report) == 3


def test_dry_run_neither_executes_commands_nor_launches_browser(capsys) -> None:
    executed = False
    launched = False

    def runner(_command: quality_gate.GateCommand) -> int:
        nonlocal executed
        executed = True
        return 0

    def preflight() -> bool:
        nonlocal launched
        launched = True
        return True

    result = quality_gate.execute(
        _args(dry_run=True),
        command_runner=runner,
        preflight=preflight,
    )

    assert result == 0
    assert executed is False
    assert launched is False
    output = capsys.readouterr().out
    assert "Playwright preflight" in output
    assert "--dist=loadscope" in output
    assert "Quality gate: DRY RUN" in output


def test_ui_phase_fails_when_junit_reports_a_skip(capsys) -> None:
    def runner(command: quality_gate.GateCommand) -> int:
        report_argument = next(argument for argument in command.argv if argument.startswith("--junitxml="))
        report = Path(report_argument.partition("=")[2])
        report.write_text('<testsuite tests="1" skipped="1"/>', encoding="utf-8")
        return 0

    result = quality_gate.execute(
        _args(phase=["ui"]),
        command_runner=runner,
        preflight=lambda: True,
    )

    assert result == 1
    assert "1 UI test(s) skipped" in capsys.readouterr().err


def test_requested_phases_keep_canonical_order() -> None:
    assert quality_gate.selected_phases(["ui", "static", "ui"]) == ("static", "ui")
    assert quality_gate.selected_phases(["all"]) == quality_gate.PHASES


def test_more_ui_workers_than_modules_is_rejected(capsys) -> None:
    result = quality_gate.execute(_args(phase=["ui"], ui_workers=13))

    assert result == 2
    assert "cannot exceed 12" in capsys.readouterr().err
