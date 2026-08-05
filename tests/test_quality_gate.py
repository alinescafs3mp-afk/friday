from __future__ import annotations

import argparse
from pathlib import Path

from tools import quality_gate


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


def test_non_ui_tests_exclude_all_nine_browser_modules() -> None:
    command = quality_gate.non_ui_command(workers=12, python="python")

    assert len(quality_gate.UI_TEST_MODULES) == 9
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
    command = quality_gate.ui_command(report_path="report.xml", workers=9, python="python")

    assert command.argv[6:9] == ("-n", "9", "--dist=loadscope")
    assert command.argv[-9:] == quality_gate.UI_TEST_MODULES
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
    result = quality_gate.execute(_args(phase=["ui"], ui_workers=10))

    assert result == 2
    assert "cannot exceed 9" in capsys.readouterr().err
