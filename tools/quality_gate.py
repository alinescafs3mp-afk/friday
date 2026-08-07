#!/usr/bin/env python3
"""Canonical, cross-platform quality gate for Friday.

The runner keeps browser tests out of the general pytest pool: several UI test
modules own a process-wide HTTP server fixture, so xdist must keep every module
on one worker.  The separate UI phase also makes an unavailable browser, or any
other skipped UI test, a gate failure instead of a silent loss of coverage.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PHASES = ("static", "tests", "ui")
UI_TEST_MODULES = (
    "tests/test_admin_ui_activity.py",
    "tests/test_admin_ui_chats.py",
    "tests/test_admin_ui_keeps_the_open_tab.py",
    "tests/test_admin_ui_relation_review.py",
    "tests/test_admin_ui_resolution_queue.py",
    "tests/test_admin_ui_sources_tab.py",
    "tests/test_admin_ui_timeline.py",
    "tests/test_the_big_picture_is_drawn_on_canvas.py",
    "tests/test_the_graph_is_alive_and_remembers_the_view.py",
    "tests/test_the_graph_shows_the_path_not_just_the_hit.py",
    "tests/test_the_graph_tab_can_be_navigated.py",
)


@dataclass(frozen=True)
class GateCommand:
    name: str
    argv: tuple[str, ...]


def static_commands(python: str = sys.executable) -> tuple[GateCommand, ...]:
    """Return the static checks in their canonical order."""

    pycache = str(Path(tempfile.gettempdir()) / "friday-quality-pycache")
    return (
        GateCommand("whitespace errors", ("git", "diff", "--check")),
        GateCommand("ruff lint", (python, "-m", "ruff", "check", ".")),
        GateCommand(
            "ruff format",
            (python, "-m", "ruff", "format", "--check", "friday", "tests", "tools"),
        ),
        GateCommand("mypy", (python, "-m", "mypy", "friday")),
        GateCommand(
            "compileall",
            (
                python,
                "-X",
                f"pycache_prefix={pycache}",
                "-m",
                "compileall",
                "-q",
                "-f",
                "friday",
                "tests",
                "tools",
            ),
        ),
        GateCommand(
            "bandit (HIGH only)",
            (
                python,
                "-m",
                "bandit",
                "-r",
                "friday",
                "-q",
                "--severity-level",
                "high",
            ),
        ),
        GateCommand("admin JavaScript syntax", ("node", "--check", "friday/admin_ui/static/app.js")),
        # Раскладка графа — отдельный поставляемый файл. Без собственной строки
        # здесь он поехал бы в браузер непроверенным: `app.js` его не импортирует,
        # а подключает страница.
        GateCommand(
            "graph layout JavaScript syntax",
            ("node", "--check", "friday/admin_ui/static/graph-layout.js"),
        ),
    )


def non_ui_command(*, workers: int, python: str = sys.executable) -> GateCommand:
    """Build the parallel pytest command with all browser modules excluded."""

    ignores = tuple(f"--ignore={module}" for module in UI_TEST_MODULES)
    return GateCommand(
        "non-UI tests",
        (
            python,
            "-m",
            "pytest",
            "-q",
            "tests",
            "-n",
            str(workers),
            "--dist=load",
            *ignores,
        ),
    )


def ui_command(*, report_path: str | Path, workers: int, python: str = sys.executable) -> GateCommand:
    """Build the isolated UI command.

    ``loadscope`` keeps every module, including its server fixture, on a single
    worker.  One worker deliberately disables xdist and is the safe fallback for
    machines on which even separate fixed ports are undesirable.
    """

    distribution = ("-n", "0") if workers == 1 else ("-n", str(workers), "--dist=loadscope")
    return GateCommand(
        "UI tests",
        (
            python,
            "-m",
            "pytest",
            "-q",
            "-r",
            "s",
            *distribution,
            f"--junitxml={report_path}",
            *UI_TEST_MODULES,
        ),
    )


def _display_command(argv: Sequence[str]) -> str:
    if os.name == "nt":
        return subprocess.list2cmdline(argv)
    import shlex

    return shlex.join(argv)


def run_command(command: GateCommand) -> int:
    print(f"\n[{command.name}]\n$ {_display_command(command.argv)}", flush=True)
    try:
        completed = subprocess.run(command.argv, cwd=ROOT, check=False)
    except OSError as exc:
        print(f"FAILED: cannot execute {command.argv[0]}: {exc}", file=sys.stderr)
        return 126
    return completed.returncode


def playwright_preflight() -> bool:
    """Prove that both the Python package and the Chromium binary are usable."""

    print("\n[Playwright preflight]", flush=True)
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            browser.close()
    except Exception as exc:
        print(f"FAILED: Playwright Chromium is unavailable: {exc}", file=sys.stderr)
        print(
            "Install the development dependencies and then run "
            f"'{_display_command((sys.executable, '-m', 'playwright', 'install', 'chromium'))}'.",
            file=sys.stderr,
        )
        return False
    print("Playwright Chromium: OK")
    return True


def junit_skip_count(report_path: str | Path) -> int:
    """Return skipped UI tests; a malformed/missing report is a gate error."""

    path = Path(report_path)
    if not path.is_file():
        raise ValueError(f"pytest did not create {path}")
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError as exc:
        raise ValueError(f"pytest created invalid JUnit XML at {path}: {exc}") from exc
    return sum(int(suite.attrib.get("skipped", "0")) for suite in root.iter("testsuite"))


def selected_phases(requested: Sequence[str] | None) -> tuple[str, ...]:
    if not requested or "all" in requested:
        return PHASES
    requested_set = set(requested)
    return tuple(phase for phase in PHASES if phase in requested_set)


def _positive_workers(value: str) -> int:
    try:
        workers = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("worker count must be an integer") from exc
    if workers < 1:
        raise argparse.ArgumentTypeError("worker count must be at least one")
    return workers


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--phase",
        action="append",
        choices=("all", *PHASES),
        help="phase to run; repeat to select several (default: all)",
    )
    parser.add_argument(
        "--workers",
        type=_positive_workers,
        default=12,
        help="workers for non-UI tests (default: 12)",
    )
    parser.add_argument(
        "--ui-workers",
        type=_positive_workers,
        default=len(UI_TEST_MODULES),
        help="UI workers; use 1 for a serial browser run (default: 9)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the selected checks without executing them",
    )
    return parser


def execute(
    args: argparse.Namespace,
    *,
    command_runner: Callable[[GateCommand], int] | None = None,
    preflight: Callable[[], bool] | None = None,
) -> int:
    runner = command_runner or run_command
    browser_preflight = preflight or playwright_preflight
    phases = selected_phases(args.phase)

    if args.ui_workers > len(UI_TEST_MODULES):
        print(
            f"FAILED: --ui-workers cannot exceed {len(UI_TEST_MODULES)} (one worker per UI module)",
            file=sys.stderr,
        )
        return 2

    if "static" in phases:
        for command in static_commands():
            if args.dry_run:
                print(f"[{command.name}] {_display_command(command.argv)}")
            elif runner(command) != 0:
                return 1

    if "tests" in phases:
        command = non_ui_command(workers=args.workers)
        if args.dry_run:
            print(f"[{command.name}] {_display_command(command.argv)}")
        elif runner(command) != 0:
            return 1

    if "ui" in phases:
        if args.dry_run:
            print("[Playwright preflight] launch headless Chromium")
            command = ui_command(report_path="<temporary>/ui-results.xml", workers=args.ui_workers)
            print(f"[{command.name}] {_display_command(command.argv)}")
        else:
            if not browser_preflight():
                return 1
            with tempfile.TemporaryDirectory(prefix="friday-quality-gate-") as tmp_dir:
                report_path = Path(tmp_dir) / "ui-results.xml"
                command = ui_command(report_path=report_path, workers=args.ui_workers)
                if runner(command) != 0:
                    return 1
                try:
                    skipped = junit_skip_count(report_path)
                except ValueError as exc:
                    print(f"FAILED: {exc}", file=sys.stderr)
                    return 1
                if skipped:
                    print(f"FAILED: {skipped} UI test(s) skipped", file=sys.stderr)
                    return 1

    outcome = "DRY RUN" if args.dry_run else "PASS"
    print(f"\nQuality gate: {outcome}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return execute(args)


if __name__ == "__main__":
    raise SystemExit(main())
