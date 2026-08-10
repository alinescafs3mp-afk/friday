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
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field
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
    "tests/test_the_graph_shows_two_time_axes_and_parallel_edges.py",
    "tests/test_the_graph_tab_can_be_navigated.py",
)

# A shell used to operate Friday commonly exports absolute runtime paths.  A
# pytest process must not inherit any of them: collection imports happen before
# per-test fixtures can replace ``FRIDAY_HOME``, and one eager settings import
# would otherwise be enough to open the live database.  Remove both the current
# and compatibility names so the isolated home remains the only path authority.
_RUNTIME_PATH_SELECTOR_SUFFIXES = (
    "BACKEND_CA_FILE",
    "BACKUPS_DIR",
    "BACKUP_ENCRYPTION_KEY_FILE",
    "BACKUP_MIRROR_DIR",
    "CACHE_DIR",
    "DATA_DIR",
    "EXPORTS_DIR",
    "FILES_DIR",
    "LOG_DIR",
    "MEMORY_VAULT_DIR",
    "MODEL_ROOT",
    "SSL_CERTFILE",
    "SSL_KEYFILE",
    "STATE_DIR",
    "TTS_DOWNLOAD_ROOT",
    "WHISPER_DOWNLOAD_ROOT",
)
_RUNTIME_ENV_PREFIXES = ("FRIDAY_", "JERICHO_")


@dataclass(frozen=True)
class GateCommand:
    name: str
    argv: tuple[str, ...]
    environment: Mapping[str, str] | None = field(default=None, repr=False, compare=False)


def _command_with_environment(
    command: GateCommand,
    environment: Mapping[str, str],
) -> GateCommand:
    return GateCommand(command.name, command.argv, environment)


def _private_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)


@contextmanager
def _isolated_test_environment() -> Iterator[dict[str, str]]:
    """Yield one private, non-live environment for pytest collection and runs."""

    with tempfile.TemporaryDirectory(prefix="friday-quality-home-") as temporary:
        scratch = Path(temporary).resolve()
        scratch.chmod(0o700)
        home = scratch / "home"
        config = home / "config"
        _private_directory(home)
        _private_directory(config)
        env_file = config / "empty.env"
        descriptor = os.open(env_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(descriptor)
        env_file.chmod(0o600)

        environment = dict(os.environ)
        for prefix in _RUNTIME_ENV_PREFIXES:
            for suffix in _RUNTIME_PATH_SELECTOR_SUFFIXES:
                environment.pop(prefix + suffix, None)
        home_value = str(home)
        env_file_value = str(env_file)
        environment.update(
            {
                # Set both names: a test which deliberately removes the current
                # name must still fall back to the same scratch boundary, never
                # to an operator setting inherited from the launching shell.
                "FRIDAY_HOME": home_value,
                "JERICHO_HOME": home_value,
                "FRIDAY_ENV_FILE": env_file_value,
                "JERICHO_ENV_FILE": env_file_value,
                # Empty is the documented "derive from STATE_DIR" database
                # selector.  Keeping the key present also prevents an env file
                # loaded by a test from silently restoring an absolute path.
                "FRIDAY_DATABASE_PATH": "",
                "JERICHO_DATABASE_PATH": "",
                "FRIDAY_DATABASE_MUST_EXIST": "0",
                "JERICHO_DATABASE_MUST_EXIST": "0",
            }
        )
        yield environment


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
        completed = subprocess.run(
            command.argv,
            cwd=ROOT,
            check=False,
            env=dict(command.environment) if command.environment is not None else None,
        )
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

    dynamic_phases = {"tests", "ui"}.intersection(phases)
    environment_context = (
        _isolated_test_environment() if dynamic_phases and not args.dry_run else nullcontext(None)
    )
    with environment_context as test_environment:
        if "tests" in phases:
            command = non_ui_command(workers=args.workers)
            if test_environment is not None:
                command = _command_with_environment(command, test_environment)
            if args.dry_run:
                print(f"[{command.name}] {_display_command(command.argv)}")
            elif runner(command) != 0:
                return 1

        if "ui" in phases and args.dry_run:
            print("[Playwright preflight] launch headless Chromium")
            command = ui_command(report_path="<temporary>/ui-results.xml", workers=args.ui_workers)
            print(f"[{command.name}] {_display_command(command.argv)}")
        elif "ui" in phases:
            if not browser_preflight():
                return 1
            with tempfile.TemporaryDirectory(prefix="friday-quality-gate-") as tmp_dir:
                report_path = Path(tmp_dir) / "ui-results.xml"
                command = ui_command(report_path=report_path, workers=args.ui_workers)
                if test_environment is not None:
                    command = _command_with_environment(command, test_environment)
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
