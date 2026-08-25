#!/usr/bin/env python3
"""Canonical, cross-platform quality gate for Friday.

The runner keeps browser tests out of the general pytest pool: several UI test
modules own a process-wide HTTP server fixture, so xdist must keep every module
on one worker.  The separate UI phase also makes an unavailable browser, or any
other skipped UI test, a gate failure instead of a silent loss of coverage.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from collections import Counter
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

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
_TEST_ASSET_ENV_ALIASES = {
    "FRIDAY_REAL_SYNCTHING_BINARY": "QUALITY_GATE_SYNCTHING_BINARY",
    "FRIDAY_SYNCTHING_AMD64_TARBALL": "QUALITY_GATE_SYNCTHING_AMD64_TARBALL",
}
_SCHEMA_FIXTURE_DIRECTORY = ROOT / "tests" / "fixtures" / "schemas"
_SCHEMA_FIXTURE_MANIFEST = "manifest.json"
_SCHEMA_FIXTURE_NAME = re.compile(r"schema-[0-9]+\.sqlite3\.gz").fullmatch
_SAFE_SCHEMA_FIXTURE_MODES = frozenset({0o400, 0o440, 0o444, 0o600, 0o640, 0o644, 0o660, 0o664})
_NODEID_PROPERTY = "friday_nodeid"
_COLLECTION_OPTION = "--friday-collection-manifest"
_COLLECTIONS_BY_WORKER: dict[str, tuple[str, ...]] = {}
_COLLECTION_SKIPS: list[str] = []
_COLLECTION_DESELECTED: list[str] = []
_COLLECTION_PROBLEMS_BY_WORKER: dict[str, tuple[int, int]] = {}
_SERIAL_COLLECTION: tuple[str, ...] | None = None


@dataclass(frozen=True)
class GateCommand:
    name: str
    argv: tuple[str, ...]
    environment: Mapping[str, str] | None = field(default=None, repr=False, compare=False)


@dataclass(frozen=True)
class JUnitSummary:
    """Authoritative aggregate from one pytest JUnit report."""

    tests: int
    failures: int
    errors: int
    skipped: int
    testcases: int
    nodeids: tuple[str, ...]


@dataclass(frozen=True)
class SchemaFixture:
    name: str
    sha256: str


def _strict_json_object(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for name, value in pairs:
        if name in result:
            raise ValueError(f"duplicate JSON key: {name}")
        result[name] = value
    return result


def _command_with_environment(
    command: GateCommand,
    environment: Mapping[str, str],
) -> GateCommand:
    return GateCommand(command.name, command.argv, environment)


def pytest_addoption(parser: Any) -> None:
    """Register the private exact-collection output used by the gate."""

    parser.addoption(
        _COLLECTION_OPTION,
        action="store",
        default="",
        help="private Friday quality-gate collection manifest",
    )


def pytest_sessionstart(session: Any) -> None:
    """Reset process-local plugin evidence before one pytest session."""

    global _SERIAL_COLLECTION
    if not session.config.getoption(_COLLECTION_OPTION):
        return
    _COLLECTIONS_BY_WORKER.clear()
    _COLLECTION_SKIPS.clear()
    _COLLECTION_DESELECTED.clear()
    _COLLECTION_PROBLEMS_BY_WORKER.clear()
    _SERIAL_COLLECTION = None


def pytest_collection_modifyitems(items: Sequence[Any]) -> None:
    """Put each exact pytest nodeid into the corresponding JUnit testcase."""

    for item in items:
        properties = item.user_properties
        if any(name == _NODEID_PROPERTY for name, _value in properties):
            raise RuntimeError(f"duplicate {_NODEID_PROPERTY} property on {item.nodeid}")
        properties.append((_NODEID_PROPERTY, item.nodeid))


def _write_collection_manifest(path: str, nodeids: Sequence[str]) -> None:
    payload = json.dumps(
        {"version": 1, "nodeids": list(nodeids)},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
        0o600,
    )
    try:
        remaining = memoryview(payload)
        while remaining:
            written = os.write(descriptor, remaining)
            if written < 1:
                raise OSError("short write while creating collection manifest")
            remaining = remaining[written:]
    finally:
        os.close(descriptor)


def pytest_collection_finish(session: Any) -> None:
    """Remember a serial collection after pytest has applied deselection."""

    global _SERIAL_COLLECTION
    path = session.config.getoption(_COLLECTION_OPTION)
    requested = session.config.getoption("numprocesses", default=0)
    parallel = isinstance(requested, int) and requested > 0
    if path and not hasattr(session.config, "workerinput") and not parallel:
        _SERIAL_COLLECTION = tuple(item.nodeid for item in session.items)


def pytest_xdist_node_collection_finished(node: Any, ids: Sequence[str]) -> None:
    """Remember each xdist worker's independently collected identity list."""

    _COLLECTIONS_BY_WORKER[node.gateway.id] = tuple(ids)


def pytest_collectreport(report: Any) -> None:
    """Make module/package collection skips terminal instead of invisible."""

    if report.skipped:
        _COLLECTION_SKIPS.append(report.nodeid)


def pytest_deselected(items: Sequence[Any]) -> None:
    """Make plugin/conftest deselection terminal in every canonical selection."""

    _COLLECTION_DESELECTED.extend(item.nodeid for item in items)


def pytest_testnodedown(node: Any, error: object) -> None:
    """Import worker-local collection failures into the xdist controller."""

    worker_id = node.gateway.id
    if error is not None:
        _COLLECTION_PROBLEMS_BY_WORKER[worker_id] = (1, 1)
        return
    payload = node.workeroutput.get("friday_collection_problems")
    if (
        not isinstance(payload, dict)
        or set(payload) != {"skipped", "deselected"}
        or not all(type(value) is int and value >= 0 for value in payload.values())
    ):
        _COLLECTION_PROBLEMS_BY_WORKER[worker_id] = (1, 1)
        return
    skipped = payload["skipped"]
    deselected = payload["deselected"]
    _COLLECTION_PROBLEMS_BY_WORKER[worker_id] = (skipped, deselected)


def pytest_sessionfinish(session: Any, exitstatus: int) -> None:
    """Emit an xdist collection manifest only when every worker agreed."""

    path = session.config.getoption(_COLLECTION_OPTION)
    if path and hasattr(session.config, "workerinput"):
        session.config.workeroutput["friday_collection_problems"] = {
            "skipped": len(_COLLECTION_SKIPS),
            "deselected": len(_COLLECTION_DESELECTED),
        }
        if _COLLECTION_SKIPS or _COLLECTION_DESELECTED:
            session.exitstatus = 1
        return
    if path and (_COLLECTION_SKIPS or _COLLECTION_DESELECTED):
        session.exitstatus = 1
        return
    if not path:
        return
    requested = session.config.getoption("numprocesses", default=0)
    if not isinstance(requested, int) or requested < 1:
        if _SERIAL_COLLECTION and exitstatus in {0, 1}:
            _write_collection_manifest(path, _SERIAL_COLLECTION)
        elif exitstatus in {0, 1}:
            session.exitstatus = 1
        return
    if len(_COLLECTIONS_BY_WORKER) != requested:
        session.exitstatus = 1
        return
    if set(_COLLECTION_PROBLEMS_BY_WORKER) != set(_COLLECTIONS_BY_WORKER):
        session.exitstatus = 1
        return
    if any(skipped or deselected for skipped, deselected in _COLLECTION_PROBLEMS_BY_WORKER.values()):
        session.exitstatus = 1
        return
    collections = tuple(_COLLECTIONS_BY_WORKER.values())
    if not collections or not collections[0] or any(nodeids != collections[0] for nodeids in collections[1:]):
        session.exitstatus = 1
        return
    if exitstatus not in {0, 1}:
        return
    _write_collection_manifest(path, collections[0])


def _private_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)


def _descriptor_identity(details: os.stat_result) -> tuple[int, int]:
    return details.st_dev, details.st_ino


def _validate_repository_descriptor(
    *,
    name: str,
    before: os.stat_result,
    opened: os.stat_result,
    current: os.stat_result,
) -> None:
    """Reject mutable aliases and unsafe metadata around one repository input."""

    details = (before, opened, current)
    if any(not stat.S_ISREG(item.st_mode) for item in details):
        raise RuntimeError(f"repository input is not a regular file: {name}")
    if _descriptor_identity(before) != _descriptor_identity(opened):
        raise RuntimeError(f"repository input changed while opening: {name}")
    if _descriptor_identity(opened) != _descriptor_identity(current):
        raise RuntimeError(f"repository input identity changed after opening: {name}")
    if any(item.st_nlink != 1 for item in details):
        raise RuntimeError(f"repository input has multiple hard links: {name}")
    if len({item.st_uid for item in details}) != 1:
        raise RuntimeError(f"repository input owner changed while open: {name}")
    if hasattr(os, "getuid") and any(item.st_uid != os.getuid() for item in details):
        raise RuntimeError(f"repository input has an unexpected owner: {name}")
    modes = {stat.S_IMODE(item.st_mode) for item in details}
    if len(modes) != 1 or not modes.issubset(_SAFE_SCHEMA_FIXTURE_MODES):
        raise RuntimeError(f"repository input has unsafe mode: {name}")


@contextmanager
def _verified_repository_descriptor(directory: Path, directory_fd: int, name: str) -> Iterator[int]:
    """Open a same-owner repository file without following a final symlink."""

    before = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if os.name != "nt" and not nofollow:
        raise RuntimeError("O_NOFOLLOW is required for schema fixture verification")
    flags |= nofollow
    descriptor = os.open(name, flags, dir_fd=directory_fd)
    try:
        opened = os.fstat(descriptor)
        current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        _validate_repository_descriptor(
            name=str(directory / name),
            before=before,
            opened=opened,
            current=current,
        )
        try:
            yield descriptor
        finally:
            opened_after = os.fstat(descriptor)
            current_after = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            _validate_repository_descriptor(
                name=str(directory / name),
                before=opened,
                opened=opened_after,
                current=current_after,
            )
    finally:
        os.close(descriptor)


def _descriptor_bytes(descriptor: int) -> bytes:
    os.lseek(descriptor, 0, os.SEEK_SET)
    chunks: list[bytes] = []
    while chunk := os.read(descriptor, 1024 * 1024):
        chunks.append(chunk)
    os.lseek(descriptor, 0, os.SEEK_SET)
    return b"".join(chunks)


def _schema_fixture_manifest(directory: Path, directory_fd: int) -> tuple[SchemaFixture, ...]:
    with _verified_repository_descriptor(directory, directory_fd, _SCHEMA_FIXTURE_MANIFEST) as source:
        try:
            payload = json.loads(_descriptor_bytes(source), object_pairs_hook=_strict_json_object)
        except (UnicodeDecodeError, ValueError) as exc:
            raise RuntimeError("schema fixture manifest is invalid") from exc
    if not isinstance(payload, dict) or set(payload) != {"version", "fixtures"}:
        raise RuntimeError("schema fixture manifest has an unsupported shape")
    if payload["version"] != 1 or not isinstance(payload["fixtures"], list):
        raise RuntimeError("schema fixture manifest has an unsupported version")

    fixtures: list[SchemaFixture] = []
    for raw in payload["fixtures"]:
        if not isinstance(raw, dict) or set(raw) != {"name", "sha256"}:
            raise RuntimeError("schema fixture manifest entry has an unsupported shape")
        name = raw["name"]
        digest = raw["sha256"]
        if not isinstance(name, str) or _SCHEMA_FIXTURE_NAME(name) is None:
            raise RuntimeError("schema fixture manifest contains an invalid filename")
        if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            raise RuntimeError("schema fixture manifest contains an invalid SHA-256")
        fixtures.append(SchemaFixture(name=name, sha256=digest))

    names = [fixture.name for fixture in fixtures]
    if not fixtures or names != sorted(names) or len(names) != len(set(names)):
        raise RuntimeError("schema fixture manifest names must be non-empty, unique, and sorted")
    return tuple(fixtures)


def _prepare_synthetic_backup_rehearsal(home: Path) -> Path:
    """Materialize tracked, non-personal schema fixtures as supplied backups."""

    directory_fd = os.open(
        _SCHEMA_FIXTURE_DIRECTORY,
        os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_DIRECTORY", 0),
    )
    try:
        fixtures = _schema_fixture_manifest(_SCHEMA_FIXTURE_DIRECTORY, directory_fd)
        expected_names = {fixture.name for fixture in fixtures}
        observed_names = {
            entry.name
            for entry in os.scandir(_SCHEMA_FIXTURE_DIRECTORY)
            if entry.name.startswith("schema-") and entry.name.endswith(".sqlite3.gz")
        }
        if observed_names != expected_names:
            missing = sorted(expected_names - observed_names)
            unexpected = sorted(observed_names - expected_names)
            raise RuntimeError(
                "schema fixture set differs from the checked manifest: "
                f"missing={missing}, unexpected={unexpected}"
            )

        backup_directory = home / "synthetic-backups"
        _private_directory(backup_directory)
        for fixture in fixtures:
            destination = backup_directory / fixture.name.removesuffix(".gz")
            target_descriptor = os.open(
                destination,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
                0o600,
            )
            try:
                with _verified_repository_descriptor(
                    _SCHEMA_FIXTURE_DIRECTORY,
                    directory_fd,
                    fixture.name,
                ) as source_descriptor:
                    digest = hashlib.sha256()
                    os.lseek(source_descriptor, 0, os.SEEK_SET)
                    with tempfile.TemporaryFile(prefix="friday-schema-compressed-") as verified_source:
                        while chunk := os.read(source_descriptor, 1024 * 1024):
                            digest.update(chunk)
                            verified_source.write(chunk)
                        if digest.hexdigest() != fixture.sha256:
                            raise RuntimeError(f"schema fixture SHA-256 mismatch: {fixture.name}")
                        verified_source.seek(0)
                        with (
                            gzip.GzipFile(fileobj=verified_source, mode="rb") as source,
                            os.fdopen(target_descriptor, "wb") as target,
                        ):
                            target_descriptor = -1
                            shutil.copyfileobj(source, target)
            except (gzip.BadGzipFile, EOFError, OSError) as exc:
                raise RuntimeError(f"schema fixture is not a valid gzip archive: {fixture.name}") from exc
            finally:
                if target_descriptor >= 0:
                    os.close(target_descriptor)
            destination.chmod(0o600)
        return backup_directory
    finally:
        os.close(directory_fd)


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
        backup_directory = _prepare_synthetic_backup_rehearsal(home)

        environment = dict(os.environ)
        test_assets = {
            alias: value
            for source, alias in _TEST_ASSET_ENV_ALIASES.items()
            if (value := environment.get(source, "").strip())
        }
        for name in tuple(environment):
            if name.startswith(_RUNTIME_ENV_PREFIXES):
                environment.pop(name, None)
        environment.pop("PYTEST_ADDOPTS", None)
        environment.pop("PYTHONPATH", None)
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
                "FRIDAY_LLM_ENABLED": "0",
                "FRIDAY_EMBEDDINGS_ENABLED": "0",
                "FRIDAY_WORKERS_ENABLED": "0",
                "FRIDAY_CODE_EXECUTION_ENABLED": "0",
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONHASHSEED": "0",
                # Exercise the complete directory-based restore rehearsal with
                # tracked synthetic databases.  Never inherit or inspect an
                # operator's real backup directory in an offline quality run.
                "FRIDAY_TEST_BACKUPS_DIR": str(backup_directory),
                **test_assets,
            }
        )
        yield environment


def static_commands(python: str = sys.executable) -> tuple[GateCommand, ...]:
    """Return the static checks in their canonical order."""

    pycache = str(Path(tempfile.gettempdir()) / "friday-quality-pycache")
    return (
        GateCommand(
            "quality toolchain",
            (python, "tools/quality_toolchain_preflight.py"),
        ),
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


def collection_command(
    *,
    manifest_path: str | Path,
    basetemp_path: str | Path | None = None,
    python: str = sys.executable,
) -> GateCommand:
    """Collect the complete canonical test inventory without running a test."""

    return GateCommand(
        "all-tests collection",
        (
            python,
            "-m",
            "pytest",
            "-q",
            "--collect-only",
            "-o",
            "addopts=",
            "-p",
            "no:cacheprovider",
            "-p",
            "tools.quality_gate",
            "tests",
            "-n",
            "0",
            f"{_COLLECTION_OPTION}={manifest_path}",
            *((f"--basetemp={basetemp_path}",) if basetemp_path is not None else ()),
        ),
    )


def non_ui_command(
    *,
    report_path: str | Path,
    collection_path: str | Path,
    workers: int,
    basetemp_path: str | Path | None = None,
    python: str = sys.executable,
) -> GateCommand:
    """Build the parallel pytest command with all browser modules excluded."""

    ignores = tuple(f"--ignore={module}" for module in UI_TEST_MODULES)
    distribution = ("-n", "0") if workers == 1 else ("-n", str(workers), "--dist=load")
    return GateCommand(
        "non-UI tests",
        (
            python,
            "-m",
            "pytest",
            "-q",
            "-r",
            "a",
            "-o",
            "addopts=",
            "-p",
            "no:cacheprovider",
            "-p",
            "tools.quality_gate",
            "tests",
            *distribution,
            f"--junitxml={report_path}",
            f"{_COLLECTION_OPTION}={collection_path}",
            *((f"--basetemp={basetemp_path}",) if basetemp_path is not None else ()),
            *ignores,
        ),
    )


def ui_command(
    *,
    report_path: str | Path,
    collection_path: str | Path,
    workers: int,
    basetemp_path: str | Path | None = None,
    python: str = sys.executable,
) -> GateCommand:
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
            "-o",
            "addopts=",
            "-p",
            "no:cacheprovider",
            "-p",
            "tools.quality_gate",
            *distribution,
            f"--junitxml={report_path}",
            f"{_COLLECTION_OPTION}={collection_path}",
            *((f"--basetemp={basetemp_path}",) if basetemp_path is not None else ()),
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


def _xml_local_name(tag: str) -> str:
    return tag.rpartition("}")[2]


def _junit_count(suite: ET.Element, attribute: str, *, path: Path) -> int:
    raw = suite.attrib.get(attribute)
    if raw is None:
        raise ValueError(f"JUnit suite in {path} has no {attribute!r} count")
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"JUnit suite in {path} has invalid {attribute!r} count {raw!r}") from exc
    if value < 0:
        raise ValueError(f"JUnit suite in {path} has negative {attribute!r} count")
    return value


def junit_summary(report_path: str | Path) -> JUnitSummary:
    """Parse and cross-check one complete pytest JUnit report.

    A zero pytest exit status is not proof that all selected tests ran: pytest
    deliberately exits zero when tests skip.  The canonical gate therefore
    requires a structurally complete report for every pytest phase and checks
    both suite counters and individual testcase outcome elements.
    """

    path = Path(report_path)
    if not path.is_file():
        raise ValueError(f"pytest did not create {path}")
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError as exc:
        raise ValueError(f"pytest created invalid JUnit XML at {path}: {exc}") from exc

    root_name = _xml_local_name(root.tag)
    if root_name == "testsuite":
        suites = [root]
    elif root_name == "testsuites":
        suites = [child for child in root if _xml_local_name(child.tag) == "testsuite"]
        unsupported = [child for child in root if _xml_local_name(child.tag) not in {"testsuite"}]
        if unsupported:
            raise ValueError(f"pytest JUnit XML at {path} has unsupported root children")
    else:
        raise ValueError(f"pytest JUnit XML at {path} has unsupported root {root_name!r}")
    all_aggregates = [element for element in root.iter() if _xml_local_name(element.tag) == "testsuites"]
    if (root_name == "testsuite" and all_aggregates) or (
        root_name == "testsuites" and (len(all_aggregates) != 1 or all_aggregates[0] is not root)
    ):
        raise ValueError(f"pytest JUnit XML at {path} contains unsupported nested test aggregates")
    if not suites:
        raise ValueError(f"pytest JUnit XML at {path} contains no test suite")
    all_suites = [element for element in root.iter() if _xml_local_name(element.tag) == "testsuite"]
    if len(all_suites) != len(suites) or {id(suite) for suite in all_suites} != {
        id(suite) for suite in suites
    }:
        raise ValueError(f"pytest JUnit XML at {path} contains unsupported nested test suites")

    direct_testcases = [
        testcase for suite in suites for testcase in suite if _xml_local_name(testcase.tag) == "testcase"
    ]
    all_testcases = [element for element in root.iter() if _xml_local_name(element.tag) == "testcase"]
    if len(direct_testcases) != len(all_testcases):
        raise ValueError(f"pytest JUnit XML at {path} contains a misplaced testcase")

    tests = sum(_junit_count(suite, "tests", path=path) for suite in suites)
    failures = sum(_junit_count(suite, "failures", path=path) for suite in suites)
    errors = sum(_junit_count(suite, "errors", path=path) for suite in suites)
    skipped = sum(_junit_count(suite, "skipped", path=path) for suite in suites)
    testcase_failures = sum(
        1 for testcase in direct_testcases for child in testcase if _xml_local_name(child.tag) == "failure"
    )
    testcase_errors = sum(
        1 for testcase in direct_testcases for child in testcase if _xml_local_name(child.tag) == "error"
    )
    testcase_skips = sum(
        1 for testcase in direct_testcases for child in testcase if _xml_local_name(child.tag) == "skipped"
    )
    allowed_outcomes = [
        child
        for testcase in direct_testcases
        for child in testcase
        if _xml_local_name(child.tag) in {"failure", "error", "skipped"}
    ]
    all_outcomes = [
        element for element in root.iter() if _xml_local_name(element.tag) in {"failure", "error", "skipped"}
    ]
    if len(allowed_outcomes) != len(all_outcomes) or {id(outcome) for outcome in allowed_outcomes} != {
        id(outcome) for outcome in all_outcomes
    }:
        raise ValueError(f"pytest JUnit XML at {path} has a misplaced testcase outcome")
    for testcase in direct_testcases:
        direct_outcomes = [
            child for child in testcase if _xml_local_name(child.tag) in {"failure", "error", "skipped"}
        ]
        nested_outcomes = [
            child
            for child in testcase.iter()
            if child is not testcase and _xml_local_name(child.tag) in {"failure", "error", "skipped"}
        ]
        if len(direct_outcomes) != len(nested_outcomes):
            raise ValueError(f"pytest JUnit XML at {path} has a misplaced testcase outcome")
        terminal_outcomes = len(direct_outcomes)
        if terminal_outcomes > 1:
            raise ValueError(f"pytest JUnit XML at {path} has multiple outcomes for one testcase")
    if tests == 0:
        raise ValueError(f"pytest JUnit XML at {path} reports zero tests")
    if tests != len(direct_testcases):
        raise ValueError(
            f"pytest JUnit XML at {path} reports {tests} tests but contains "
            f"{len(direct_testcases)} testcase elements"
        )
    if (failures, errors, skipped) != (testcase_failures, testcase_errors, testcase_skips):
        raise ValueError(
            f"pytest JUnit XML at {path} has inconsistent outcome counts: "
            f"suite={(failures, errors, skipped)}, "
            f"testcases={(testcase_failures, testcase_errors, testcase_skips)}"
        )

    aggregate_names = ("tests", "failures", "errors", "skipped")
    aggregate_present = tuple(name in root.attrib for name in aggregate_names)
    if root_name == "testsuites" and any(aggregate_present):
        if not all(aggregate_present):
            raise ValueError(f"pytest JUnit XML at {path} has a partial root aggregate")
        root_aggregate = tuple(_junit_count(root, name, path=path) for name in aggregate_names)
        if root_aggregate != (tests, failures, errors, skipped):
            raise ValueError(f"pytest JUnit XML at {path} has a contradictory root aggregate")

    nodeids: list[str] = []
    allowed_property_containers: list[ET.Element] = []
    allowed_properties: list[ET.Element] = []
    for testcase in direct_testcases:
        property_containers = [child for child in testcase if _xml_local_name(child.tag) == "properties"]
        if len(property_containers) != 1:
            raise ValueError(f"pytest JUnit XML at {path} does not contain one testcase properties container")
        allowed_property_containers.extend(property_containers)
        direct_properties = [
            property_element
            for property_element in property_containers[0]
            if _xml_local_name(property_element.tag) == "property"
        ]
        allowed_properties.extend(direct_properties)
        values = [
            property_element.attrib.get("value")
            for property_element in direct_properties
            if property_element.attrib.get("name") == _NODEID_PROPERTY
        ]
        all_nodeid_properties = [
            property_element
            for property_element in testcase.iter()
            if _xml_local_name(property_element.tag) == "property"
            and property_element.attrib.get("name") == _NODEID_PROPERTY
        ]
        if len(values) != len(all_nodeid_properties):
            raise ValueError(f"pytest JUnit XML at {path} has a misplaced exact nodeid property")
        if len(values) != 1 or not isinstance(values[0], str) or not values[0]:
            raise ValueError(
                f"pytest JUnit XML at {path} does not contain exactly one exact nodeid per testcase"
            )
        nodeids.append(values[0])
    all_property_containers = [
        element for element in root.iter() if _xml_local_name(element.tag) == "properties"
    ]
    all_properties = [element for element in root.iter() if _xml_local_name(element.tag) == "property"]
    if {id(element) for element in allowed_property_containers} != {
        id(element) for element in all_property_containers
    } or {id(element) for element in allowed_properties} != {id(element) for element in all_properties}:
        raise ValueError(f"pytest JUnit XML at {path} has misplaced testcase properties")
    duplicates = sorted(nodeid for nodeid, count in Counter(nodeids).items() if count != 1)
    if duplicates:
        raise ValueError(f"pytest JUnit XML at {path} contains duplicate nodeids: {duplicates}")
    return JUnitSummary(
        tests=tests,
        failures=failures,
        errors=errors,
        skipped=skipped,
        testcases=len(direct_testcases),
        nodeids=tuple(nodeids),
    )


def junit_skip_count(report_path: str | Path) -> int:
    """Return skipped tests while preserving the historical public helper."""

    return junit_summary(report_path).skipped


def _junit_phase_is_clean(
    report_path: str | Path,
    *,
    phase: str,
    expected_nodeids: Sequence[str],
) -> bool:
    try:
        summary = junit_summary(report_path)
    except ValueError as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return False
    if summary.failures or summary.errors or summary.skipped:
        print(
            f"FAILED: {phase} JUnit reports failures={summary.failures}, "
            f"errors={summary.errors}, skipped={summary.skipped}",
            file=sys.stderr,
        )
        return False
    if Counter(summary.nodeids) != Counter(expected_nodeids):
        print(f"FAILED: {phase} JUnit nodeids differ from its collection manifest", file=sys.stderr)
        return False
    return True


def collection_nodeids(manifest_path: str | Path) -> tuple[str, ...]:
    """Read one exact private pytest collection manifest."""

    path = Path(manifest_path)
    if not path.is_file():
        raise ValueError(f"pytest did not create collection manifest {path}")
    try:
        payload = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_strict_json_object,
        )
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise ValueError(f"pytest created invalid collection manifest at {path}") from exc
    if not isinstance(payload, dict) or set(payload) != {"version", "nodeids"}:
        raise ValueError(f"pytest collection manifest at {path} has an unsupported shape")
    raw_nodeids = payload["nodeids"]
    if payload["version"] != 1 or not isinstance(raw_nodeids, list) or not raw_nodeids:
        raise ValueError(f"pytest collection manifest at {path} is empty or unsupported")
    if not all(isinstance(nodeid, str) and nodeid for nodeid in raw_nodeids):
        raise ValueError(f"pytest collection manifest at {path} contains an invalid nodeid")
    duplicates = sorted(nodeid for nodeid, count in Counter(raw_nodeids).items() if count != 1)
    if duplicates:
        raise ValueError(f"pytest collection manifest at {path} contains duplicates: {duplicates}")
    return tuple(raw_nodeids)


def partition_collection(nodeids: Sequence[str]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Partition the all-tests collection into the exact non-UI/UI selections."""

    ui_modules = set(UI_TEST_MODULES)
    non_ui = tuple(nodeid for nodeid in nodeids if nodeid.partition("::")[0] not in ui_modules)
    ui = tuple(nodeid for nodeid in nodeids if nodeid.partition("::")[0] in ui_modules)
    if not non_ui or not ui:
        raise ValueError("canonical collection must contain both non-UI and UI tests")
    if set(non_ui).intersection(ui) or Counter((*non_ui, *ui)) != Counter(nodeids):
        raise ValueError("non-UI and UI collections are not a disjoint complete partition")
    return non_ui, ui


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
        help=(
            f"UI workers; use 1 for a serial browser run (default: {len(UI_TEST_MODULES)}, one per UI module)"
        ),
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

    static = static_commands()
    toolchain = static[0]
    if args.dry_run:
        print(f"[{toolchain.name}] {_display_command(toolchain.argv)}")
    elif runner(toolchain) != 0:
        return 1

    if "static" in phases:
        for command in static[1:]:
            if args.dry_run:
                print(f"[{command.name}] {_display_command(command.argv)}")
            elif runner(command) != 0:
                return 1

    dynamic_phases = {"tests", "ui"}.intersection(phases)
    environment_context = (
        _isolated_test_environment() if dynamic_phases and not args.dry_run else nullcontext(None)
    )
    report_context = (
        # Keep this path deliberately short: several transport tests bind an
        # AF_UNIX socket below pytest's per-worker/per-test hierarchy.
        tempfile.TemporaryDirectory(prefix="fq-")
        if dynamic_phases and not args.dry_run
        else nullcontext(None)
    )
    with environment_context as test_environment, report_context as report_directory:
        expected_non_ui: tuple[str, ...] = ()
        expected_ui: tuple[str, ...] = ()
        if dynamic_phases:
            all_collection_path: str | Path = (
                "<temporary>/all-tests-collection.json"
                if args.dry_run
                else Path(str(report_directory)) / "all-tests-collection.json"
            )
            collection_basetemp: str | Path = (
                "<temporary>/c" if args.dry_run else Path(str(report_directory)) / "c"
            )
            command = collection_command(
                manifest_path=all_collection_path,
                basetemp_path=collection_basetemp,
            )
            if test_environment is not None:
                command = _command_with_environment(command, test_environment)
            if args.dry_run:
                print(f"[{command.name}] {_display_command(command.argv)}")
            elif runner(command) != 0:
                return 1
            else:
                try:
                    all_nodeids = collection_nodeids(all_collection_path)
                    expected_non_ui, expected_ui = partition_collection(all_nodeids)
                except ValueError as exc:
                    print(f"FAILED: {exc}", file=sys.stderr)
                    return 1

        if "tests" in phases:
            report_path: str | Path = (
                "<temporary>/non-ui-results.xml"
                if args.dry_run
                else Path(str(report_directory)) / "non-ui-results.xml"
            )
            collection_path: str | Path = (
                "<temporary>/non-ui-collection.json"
                if args.dry_run
                else Path(str(report_directory)) / "non-ui-collection.json"
            )
            command = non_ui_command(
                report_path=report_path,
                collection_path=collection_path,
                workers=args.workers,
                basetemp_path=("<temporary>/n" if args.dry_run else Path(str(report_directory)) / "n"),
            )
            if test_environment is not None:
                command = _command_with_environment(command, test_environment)
            if args.dry_run:
                print(f"[{command.name}] {_display_command(command.argv)}")
            elif runner(command) != 0:
                return 1
            else:
                try:
                    selected_nodeids = collection_nodeids(collection_path)
                except ValueError as exc:
                    print(f"FAILED: {exc}", file=sys.stderr)
                    return 1
                if Counter(selected_nodeids) != Counter(expected_non_ui):
                    print(
                        "FAILED: non-UI selection differs from the canonical all-tests collection",
                        file=sys.stderr,
                    )
                    return 1
                if not _junit_phase_is_clean(
                    report_path,
                    phase="non-UI",
                    expected_nodeids=selected_nodeids,
                ):
                    return 1

        if "ui" in phases and args.dry_run:
            print("[Playwright preflight] launch headless Chromium")
            command = ui_command(
                report_path="<temporary>/ui-results.xml",
                collection_path="<temporary>/ui-collection.json",
                workers=args.ui_workers,
                basetemp_path="<temporary>/u",
            )
            print(f"[{command.name}] {_display_command(command.argv)}")
        elif "ui" in phases:
            if not browser_preflight():
                return 1
            report_path = Path(str(report_directory)) / "ui-results.xml"
            collection_path = Path(str(report_directory)) / "ui-collection.json"
            command = ui_command(
                report_path=report_path,
                collection_path=collection_path,
                workers=args.ui_workers,
                basetemp_path=Path(str(report_directory)) / "u",
            )
            if test_environment is not None:
                command = _command_with_environment(command, test_environment)
            if runner(command) != 0:
                return 1
            try:
                selected_nodeids = collection_nodeids(collection_path)
            except ValueError as exc:
                print(f"FAILED: {exc}", file=sys.stderr)
                return 1
            if Counter(selected_nodeids) != Counter(expected_ui):
                print(
                    "FAILED: UI selection differs from the canonical all-tests collection",
                    file=sys.stderr,
                )
                return 1
            if not _junit_phase_is_clean(
                report_path,
                phase="UI",
                expected_nodeids=selected_nodeids,
            ):
                return 1

    outcome = "DRY RUN" if args.dry_run else "PASS"
    print(f"\nQuality gate: {outcome}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return execute(args)


if __name__ == "__main__":
    raise SystemExit(main())
