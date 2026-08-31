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
import importlib.util
import json
import os
import platform
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import xml.etree.ElementTree as ET
from collections import Counter
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager, nullcontext, suppress
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
GIT = "/usr/bin/git" if Path("/usr/bin/git").is_file() else (shutil.which("git") or "git")
_NO_GLOBAL_GIT_ATTRIBUTES = f"core.attributesFile={os.devnull}"
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
_NODEID_PROPERTY = "friday_nodeid"
_COLLECTION_OPTION = "--friday-collection-manifest"
_SELECTION_OPTION = "--friday-tier-selection"
_INSTALLED_SITE_ENV = "FRIDAY_QUALITY_GATE_INSTALLED_SITE"
_WHEEL_NAMESPACES = ("friday", "friday_host_agent", "friday_package_broker")
_EXACT_CPU_FLOOR = 24
_EXACT_SCRATCH_FREE_FLOOR = 32 * 1024 * 1024 * 1024
_SYNCTHING_AMD64_SHA256 = "e8a08fdd8b25340aae0c0a00ab131b293830e4ea47504d4b83a82f31b52b96c4"
_MAX_COLLECTION_BYTES = 64 << 20
_MAX_COLLECTION_NODES = 100_000
_MAX_COLLECTION_NODE_BYTES = 256 << 10
_OBSERVATION_TEST_ENV = (
    "FRIDAY_TEST_BACKUPS_DIR",
    "FRIDAY_REAL_SYNCTHING_BINARY",
    "QUALITY_GATE_SYNCTHING_BINARY",
    "QUALITY_GATE_POWERSHELL_BINARY",
    "PATH",
)
_COLLECTIONS_BY_WORKER: dict[str, tuple[str, ...]] = {}
_COLLECTION_SKIPS: list[str] = []
_COLLECTION_DESELECTED: list[str] = []
_COLLECTION_PROBLEMS_BY_WORKER: dict[str, tuple[int, int]] = {}
_SERIAL_COLLECTION: tuple[str, ...] | None = None
_TIER_SELECTION: frozenset[str] | None = None


@dataclass(frozen=True)
class GateCommand:
    name: str
    argv: tuple[str, ...]
    environment: Mapping[str, str] | None = field(default=None, repr=False, compare=False)
    cwd: Path = field(default=ROOT, repr=False, compare=False)
    timeout_s: int = field(default=3600, repr=False, compare=False)


@dataclass(frozen=True)
class JUnitSummary:
    """Authoritative aggregate from one pytest JUnit report."""

    tests: int
    failures: int
    errors: int
    skipped: int
    testcases: int
    nodeids: tuple[str, ...]


def _strict_json_object(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for name, value in pairs:
        if name in result:
            raise ValueError(f"duplicate JSON key: {name}")
        result[name] = value
    return result


def _bounded_collection_nodeids(value: object) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ValueError("pytest collection contains invalid nodeids")
    result = tuple(value)
    try:
        valid = 0 < len(result) <= _MAX_COLLECTION_NODES and all(
            isinstance(nodeid, str) and bool(nodeid) and len(nodeid.encode()) <= _MAX_COLLECTION_NODE_BYTES
            for nodeid in result
        )
    except UnicodeEncodeError as exc:
        raise ValueError("pytest collection contains invalid nodeids") from exc
    if not valid:
        raise ValueError("pytest collection contains invalid nodeids")
    if len(set(result)) != len(result):
        raise ValueError("pytest collection contains duplicates")
    return result


def pytest_addoption(parser: Any) -> None:
    """Register the private exact-collection output used by the gate."""

    parser.addoption(
        _COLLECTION_OPTION,
        action="store",
        default="",
        help="private Friday quality-gate collection manifest",
    )
    parser.addoption(
        _SELECTION_OPTION,
        action="store",
        default="",
        help="private exact node selection produced from the checked-in inventory",
    )


def pytest_sessionstart(session: Any) -> None:
    """Reset process-local plugin evidence before one pytest session."""

    global _SERIAL_COLLECTION, _TIER_SELECTION
    if not session.config.getoption(_COLLECTION_OPTION):
        return
    _COLLECTIONS_BY_WORKER.clear()
    _COLLECTION_SKIPS.clear()
    _COLLECTION_DESELECTED.clear()
    _COLLECTION_PROBLEMS_BY_WORKER.clear()
    _SERIAL_COLLECTION = None
    selection_path = session.config.getoption(_SELECTION_OPTION)
    _TIER_SELECTION = frozenset(collection_nodeids(selection_path)) if selection_path else None

    installed_site = os.environ.get(_INSTALLED_SITE_ENV, "").strip()
    if installed_site:
        site = Path(installed_site)
        if not site.is_absolute() or not site.is_dir() or site.resolve(strict=True) != site:
            raise RuntimeError("installed wheel runtime is not canonical")
        _require_installed_wheel_imports(site)


def _require_installed_wheel_imports(site: Path, modules: Mapping[str, object] | None = None) -> None:
    loaded = sys.modules if modules is None else modules
    for root in _WHEEL_NAMESPACES:
        spec = importlib.util.find_spec(root)
        origin = Path(spec.origin).resolve(strict=True) if spec and spec.origin else None
        if origin != (site / root / "__init__.py").resolve(strict=True):
            raise RuntimeError(f"{root} is not imported from the clean-installed wheel")
    for name, module in tuple(loaded.items()):
        module_file = getattr(module, "__file__", None)
        if (
            module_file
            and any(name == root or name.startswith(f"{root}.") for root in _WHEEL_NAMESPACES)
            and not Path(module_file).resolve(strict=True).is_relative_to(site)
        ):
            raise RuntimeError("a shipped module escaped the clean-installed wheel")


def pytest_collection_modifyitems(items: list[Any]) -> None:
    """Put each exact pytest nodeid into the corresponding JUnit testcase."""

    if _TIER_SELECTION is not None:
        items[:] = [item for item in items if item.nodeid in _TIER_SELECTION]
    for item in items:
        properties = item.user_properties
        if any(name == _NODEID_PROPERTY for name, _value in properties):
            raise RuntimeError(f"duplicate {_NODEID_PROPERTY} property on {item.nodeid}")
        properties.append((_NODEID_PROPERTY, item.nodeid))


def _write_collection_manifest(path: str, nodeids: Sequence[str]) -> None:
    exact = _bounded_collection_nodeids(nodeids)
    payload = json.dumps(
        {"version": 1, "nodeids": list(exact)},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(payload) > _MAX_COLLECTION_BYTES:
        raise ValueError("pytest collection manifest is oversized")
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

    installed_site = os.environ.get(_INSTALLED_SITE_ENV, "").strip()
    if installed_site:
        _require_installed_wheel_imports(Path(installed_site).resolve(strict=True))
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


def _scratch_parent() -> str | None:
    if os.name == "nt":
        return None
    parent = Path(os.environ.get("QUALITY_GATE_SCRATCH_PARENT", "/var/tmp"))
    if not parent.is_absolute() or not parent.is_dir() or parent.is_symlink():
        raise RuntimeError("QUALITY_GATE_SCRATCH_PARENT must be an absolute real directory")
    return str(parent)


def _exact_host_capacity() -> dict[str, int]:
    cpu_count = (os.process_cpu_count() if hasattr(os, "process_cpu_count") else os.cpu_count()) or 0
    scratch_free = shutil.disk_usage(_scratch_parent() or tempfile.gettempdir()).free
    if cpu_count < _EXACT_CPU_FLOOR or scratch_free < _EXACT_SCRATCH_FREE_FLOOR:
        raise RuntimeError("exact-release host lacks the required CPU or scratch capacity")
    return {"effective_cpus": cpu_count, "initial_scratch_free_bytes": scratch_free}


def _descriptor_identity(details: os.stat_result) -> tuple[int, int]:
    return details.st_dev, details.st_ino


def _prepare_synthetic_backup_rehearsal(
    home: Path,
    fixture_directory: Path = _SCHEMA_FIXTURE_DIRECTORY,
) -> Path:
    """Materialize tracked, non-personal schema fixtures as supplied backups."""

    try:
        payload = json.loads(
            (fixture_directory / _SCHEMA_FIXTURE_MANIFEST).read_text(encoding="utf-8"),
            object_pairs_hook=_strict_json_object,
        )
        if (
            not isinstance(payload, dict)
            or set(payload) != {"version", "fixtures"}
            or payload["version"] != 1
            or not isinstance(payload["fixtures"], list)
            or not payload["fixtures"]
            or any(
                not isinstance(item, dict) or set(item) != {"name", "sha256"} for item in payload["fixtures"]
            )
        ):
            raise RuntimeError("schema fixture manifest has an unsupported shape")
        fixtures = payload["fixtures"]
        pairs = {item["name"]: item["sha256"] for item in fixtures}
        if (
            len(pairs) != len(fixtures)
            or tuple(pairs) != tuple(sorted(pairs))
            or any(
                not isinstance(name, str)
                or _SCHEMA_FIXTURE_NAME(name) is None
                or not isinstance(digest, str)
                or re.fullmatch(r"[0-9a-f]{64}", digest) is None
                for name, digest in pairs.items()
            )
        ):
            raise RuntimeError("schema fixture manifest entries are invalid")
        if {path.name for path in fixture_directory.glob("schema-*.sqlite3.gz")} != set(pairs):
            raise RuntimeError("schema fixture set differs from the checked manifest")
        backup_directory = home / "synthetic-backups"
        _private_directory(backup_directory)
        for name, expected in pairs.items():
            source = fixture_directory / name
            if (
                not source.is_file()
                or source.is_symlink()
                or hashlib.sha256(source.read_bytes()).hexdigest() != expected
            ):
                raise RuntimeError(f"schema fixture SHA-256 mismatch: {name}")
            with gzip.open(source, "rb") as packed:
                data = packed.read((256 << 20) + 1)
            if len(data) > 256 << 20:
                raise RuntimeError(f"schema fixture is oversized: {name}")
            destination = backup_directory / name.removesuffix(".gz")
            destination.write_bytes(data)
            destination.chmod(0o600)
        return backup_directory
    except (gzip.BadGzipFile, EOFError, OSError, TypeError, UnicodeDecodeError, ValueError) as exc:
        raise RuntimeError("schema fixture rehearsal is invalid") from exc


@contextmanager
def _isolated_test_environment(
    scratch_parent: Path | None = None,
    *,
    prepare_schema_backups: bool = True,
    source_root: Path = ROOT,
) -> Iterator[dict[str, str]]:
    """Yield one private, non-live environment for pytest collection and runs."""

    with tempfile.TemporaryDirectory(
        prefix="friday-quality-home-",
        dir=str(scratch_parent) if scratch_parent is not None else _scratch_parent(),
    ) as temporary:
        scratch = Path(temporary).resolve()
        scratch.chmod(0o700)
        home = scratch / "home"
        config = home / "config"
        test_tmp = scratch / "tmp"
        _private_directory(home)
        _private_directory(config)
        _private_directory(test_tmp)
        env_file = config / "empty.env"
        descriptor = os.open(env_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(descriptor)
        env_file.chmod(0o600)
        if prepare_schema_backups:
            backup_directory = _prepare_synthetic_backup_rehearsal(
                home, source_root / "tests" / "fixtures" / "schemas"
            )
        else:
            backup_directory = home / "synthetic-backups"
            _private_directory(backup_directory)

        environment = _git_environment()
        test_assets = {
            alias: value
            for source, alias in _TEST_ASSET_ENV_ALIASES.items()
            if (value := environment.get(source, "").strip())
        }
        installed_site = environment.get(_INSTALLED_SITE_ENV, "").strip()
        if installed_site:
            site = Path(installed_site)
            if not site.is_absolute() or site.resolve(strict=True) != site or not site.is_dir():
                raise RuntimeError("installed wheel runtime is not canonical")
            test_assets[_INSTALLED_SITE_ENV] = installed_site
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
                "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
                "PYTEST_PLUGINS": "",
                "PYTHONPYCACHEPREFIX": str(scratch / "python-cache"),
                "PYTHONSAFEPATH": "1",
                "TMPDIR": str(test_tmp),
                # Exercise the complete directory-based restore rehearsal with
                # tracked synthetic databases.  Never inherit or inspect an
                # operator's real backup directory in an offline quality run.
                "FRIDAY_TEST_BACKUPS_DIR": str(backup_directory),
                **test_assets,
            }
        )
        if installed_site:
            environment["PYTHONPATH"] = installed_site
        yield environment


def static_commands(python: str = sys.executable) -> tuple[GateCommand, ...]:
    """Return the static checks in their canonical order."""

    package_roots = ("friday", "friday_host_agent", "friday_package_broker")
    deployment = "deploy/host-control"
    module = (python, "-I", "-B", "-m")
    plans = (
        ("quality toolchain", (python, "-I", "-B", "tools/quality_toolchain_preflight.py")),
        ("whitespace errors", ("git", "diff", "--check")),
        ("ruff lint", (*module, "ruff", "check", ".")),
        ("ruff format", (*module, "ruff", "format", "--check", *package_roots, deployment, "tests", "tools")),
        ("mypy", (*module, "mypy", *package_roots, deployment)),
        (
            "bandit (HIGH only)",
            (*module, "bandit", "-r", *package_roots, deployment, "-q", "--severity-level", "high"),
        ),
        ("admin JavaScript syntax", ("node", "--check", "friday/admin_ui/static/app.js")),
        *(
            (name, ("/bin/sh", "-n", path))
            for name, path in (
                ("host-control installer shell syntax", f"{deployment}/install.sh"),
                ("host-control uninstaller shell syntax", f"{deployment}/uninstall.sh"),
                ("engineer AppArmor installer shell syntax", "deploy/engineer-mode/install-apparmor.sh"),
                ("engineer AppArmor uninstaller shell syntax", "deploy/engineer-mode/uninstall-apparmor.sh"),
                ("engineer runtime verifier shell syntax", "deploy/engineer-mode/verify-runtime.sh"),
            )
        ),
        ("graph layout JavaScript syntax", ("node", "--check", "friday/admin_ui/static/graph-layout.js")),
    )
    return tuple(GateCommand(name, argv) for name, argv in plans)


def _display_command(argv: Sequence[str]) -> str:
    if os.name == "nt":
        return subprocess.list2cmdline(argv)
    import shlex

    return shlex.join(argv)


def _kill_process_group(process: subprocess.Popen[bytes], name: str) -> bool:
    if os.name == "nt":
        try:
            process.kill()
            process.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            print(f"FAILED: {name} process survived termination", file=sys.stderr)
            return False
        return True
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        return True
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.killpg(process.pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.05)
    print(f"FAILED: {name} process group survived SIGKILL", file=sys.stderr)
    return False


def run_command(command: GateCommand) -> int:
    print(f"\n[{command.name}]\n$ {_display_command(command.argv)}", flush=True)
    try:
        process = subprocess.Popen(  # noqa: S603 - argv is the closed gate plan
            command.argv,
            cwd=command.cwd,
            env=dict(command.environment) if command.environment is not None else None,
            start_new_session=os.name != "nt",
        )
    except OSError as exc:
        print(f"FAILED: cannot execute {command.argv[0]}: {exc}", file=sys.stderr)
        return 126
    try:
        returncode = process.wait(timeout=command.timeout_s)
    except subprocess.TimeoutExpired:
        print(f"FAILED: {command.name} exceeded {command.timeout_s}s", file=sys.stderr)
        try:
            if os.name != "nt":
                os.killpg(process.pid, signal.SIGTERM)
            else:
                process.terminate()
        except ProcessLookupError:
            pass
        with suppress(subprocess.TimeoutExpired):
            process.wait(timeout=5)
        return 124 if _kill_process_group(process, command.name) else 125
    if os.name != "nt":
        try:
            os.killpg(process.pid, 0)
        except ProcessLookupError:
            pass
        else:
            print(f"FAILED: {command.name} left a child process behind", file=sys.stderr)
            return 125 if _kill_process_group(process, command.name) else 126
    return returncode


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
    try:
        with path.open("rb") as stream:
            raw = stream.read(_MAX_COLLECTION_BYTES + 1)
        if not raw or len(raw) > _MAX_COLLECTION_BYTES:
            raise ValueError("pytest collection manifest is empty or oversized")
        payload = json.loads(
            raw,
            object_pairs_hook=_strict_json_object,
        )
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise ValueError(f"pytest created invalid collection manifest at {path}") from exc
    if not isinstance(payload, dict) or set(payload) != {"version", "nodeids"}:
        raise ValueError(f"pytest collection manifest at {path} has an unsupported shape")
    raw_nodeids = payload["nodeids"]
    if payload["version"] != 1 or not isinstance(raw_nodeids, list):
        raise ValueError(f"pytest collection manifest at {path} is empty or unsupported")
    return _bounded_collection_nodeids(raw_nodeids)


def _git_environment() -> dict[str, str]:
    environment = {name: value for name, value in os.environ.items() if not name.startswith("GIT_")}
    environment.update(
        {
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    return environment


def _git_output(root: Path, *arguments: str) -> str:
    try:
        completed = subprocess.run(  # noqa: S603 - fixed Git and code-owned arguments
            (GIT, "-C", str(root), *arguments),
            capture_output=True,
            env=_git_environment(),
            timeout=60,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("Git command exceeded its bounded runtime") from exc
    if completed.returncode != 0:
        raise RuntimeError(f"Git command failed: {' '.join(arguments)}")
    if len(completed.stdout) > 16 * 1024 * 1024:
        raise RuntimeError("Git command output is oversized")
    try:
        return completed.stdout.decode("utf-8").strip()
    except UnicodeDecodeError as exc:
        raise RuntimeError("Git command output is not UTF-8") from exc


def _require_candidate_launcher(candidate_sha: str) -> None:
    paths = ("tools/quality_gate.py", "tools/quality_gate_inventory.py")
    if (
        Path(__file__).resolve(strict=True) != (ROOT / paths[0]).resolve(strict=True)
        or _git_output(ROOT, "rev-parse", "HEAD") != candidate_sha
        or _git_output(ROOT, "status", "--porcelain=v1", "--untracked-files=all", "--", *paths)
    ):
        raise RuntimeError("closed tier launcher is not the clean candidate")
    for relative in paths:
        row = _git_output(ROOT, "ls-tree", candidate_sha, "--", relative)
        fields = row.partition("\t")[0].split()
        path = ROOT / relative
        details = path.lstat()
        if (
            len(fields) != 3
            or fields[0] not in {"100644", "100755"}
            or fields[1] != "blob"
            or _git_output(ROOT, "hash-object", "--no-filters", "--", relative) != fields[2]
            or _git_output(ROOT, "ls-files", "-v", "--", relative) != f"H {relative}"
            or not stat.S_ISREG(details.st_mode)
            or details.st_nlink != 1
            or bool(details.st_mode & 0o111) != (fields[0] == "100755")
        ):
            raise RuntimeError("closed tier launcher bytes differ from the candidate")


def _candidate_inventory_loader() -> Callable[[str | os.PathLike[str]], Any]:
    path = (ROOT / "tools" / "quality_gate_inventory.py").resolve(strict=True)
    name = "_friday_candidate_quality_gate_inventory"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError("candidate inventory loader is unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(name, None)
        raise
    if Path(module.__file__).resolve(strict=True) != path:
        raise RuntimeError("closed tier imported a non-candidate inventory")
    return module.load_inventory


@contextmanager
def _candidate_projection(
    candidate_sha: str,
    scratch: Path,
    *,
    origin: Path | None = None,
    name: str = "source",
) -> Iterator[Path]:
    origin = ROOT if origin is None else origin
    source = scratch / name
    clone = GateCommand(
        "private candidate clone",
        (
            GIT,
            "clone",
            "--quiet",
            "--no-local",
            "--no-hardlinks",
            "--single-branch",
            "--no-checkout",
            "--no-tags",
            str(origin),
            str(source),
        ),
        _git_environment(),
        cwd=origin,
        timeout_s=60,
    )
    if run_command(clone) != 0:
        raise RuntimeError("private candidate clone failed")
    _git_output(source, "checkout", "--detach", candidate_sha)
    _git_output(source, "remote", "remove", "origin")
    candidate_tree = _git_output(source, "rev-parse", f"{candidate_sha}^{{tree}}")
    _require_exact_projection(source, candidate_sha, candidate_tree)
    worktree_digest = _projection_digest(source, exclude_git=True)
    git_digest = _projection_digest(source / ".git")
    yield source
    if (
        _projection_digest(source, exclude_git=True) != worktree_digest
        or _projection_digest(source / ".git") != git_digest
    ):
        raise RuntimeError("private candidate projection changed during the gate")
    _require_exact_projection(source, candidate_sha, candidate_tree)


def _require_exact_projection(source: Path, candidate_sha: str, candidate_tree: str) -> None:
    git_directory = source / ".git"
    if (
        not git_directory.is_dir()
        or git_directory.is_symlink()
        or (git_directory / "objects" / "info" / "alternates").exists()
        or _git_output(source, "rev-parse", "--git-common-dir") != ".git"
        or _git_output(source, "remote")
    ):
        raise RuntimeError("private candidate clone retained external Git authority")
    if (
        _git_output(source, "rev-parse", "HEAD") != candidate_sha
        or _git_output(source, "rev-parse", "HEAD^{tree}") != candidate_tree
        or _git_output(
            source,
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
            "--ignored=matching",
        )
    ):
        raise RuntimeError("private candidate projection changed during the gate")


def _projection_digest(root: Path, *, exclude_git: bool = False) -> str:
    if not root.is_dir() or root.is_symlink():
        raise RuntimeError("private projection root is not a real directory")
    digest = hashlib.sha256(b"friday-private-projection-v1\0")
    pending = [(root, "")]
    while pending:
        directory, prefix = pending.pop()
        with os.scandir(directory) as iterator:
            entries = sorted(iterator, key=lambda item: item.name)
        for entry in entries:
            if exclude_git and not prefix and entry.name == ".git":
                continue
            relative = f"{prefix}/{entry.name}" if prefix else entry.name
            details = entry.stat(follow_symlinks=False)
            mode = stat.S_IMODE(details.st_mode)
            kind = stat.S_IFMT(details.st_mode)
            encoded = relative.encode()
            digest.update(len(encoded).to_bytes(4, "big") + encoded)
            digest.update(kind.to_bytes(4, "big") + mode.to_bytes(4, "big"))
            if stat.S_ISDIR(details.st_mode):
                pending.append((Path(entry.path), relative))
            elif stat.S_ISLNK(details.st_mode):
                raise RuntimeError("private projection contains a symbolic link")
            elif stat.S_ISREG(details.st_mode) and details.st_nlink == 1:
                digest.update(details.st_size.to_bytes(8, "big"))
                with open(entry.path, "rb") as stream:
                    while chunk := stream.read(1024 * 1024):
                        digest.update(chunk)
            else:
                raise RuntimeError("private projection contains an unsafe filesystem entry")
    return digest.hexdigest()


def _directory_bytes(root: Path) -> int:
    total = 0
    pending = [root]
    while pending:
        directory = pending.pop()
        try:
            with os.scandir(directory) as iterator:
                entries = tuple(iterator)
        except FileNotFoundError:
            continue
        for entry in entries:
            try:
                details = entry.stat(follow_symlinks=False)
            except FileNotFoundError:
                continue
            if stat.S_ISDIR(details.st_mode):
                pending.append(Path(entry.path))
            elif stat.S_ISREG(details.st_mode):
                total += details.st_size
    return total


@contextmanager
def _scratch_peak_sampler(root: Path, interval_s: float) -> Iterator[list[int]]:
    stop = threading.Event()
    peak = [_directory_bytes(root)]
    errors: list[OSError] = []

    def sample() -> None:
        try:
            while not stop.wait(interval_s):
                peak[0] = max(peak[0], _directory_bytes(root))
        except OSError as exc:
            errors.append(exc)

    sampler = threading.Thread(target=sample, name="quality-gate-scratch", daemon=True)
    sampler.start()
    try:
        yield peak
    finally:
        stop.set()
        sampler.join(timeout=5)
    if sampler.is_alive() or errors:
        raise RuntimeError("scratch sampler failed")
    peak[0] = max(peak[0], _directory_bytes(root))


def _require_comparison_wheel(actual_sha256: str, expected_sha256: str | None) -> None:
    if expected_sha256 is not None and actual_sha256 != expected_sha256:
        raise RuntimeError("self-built wheel differs from comparison bytes")


def _stat_identity(details: os.stat_result) -> tuple[int, ...]:
    fields = "st_dev st_ino st_mode st_nlink st_uid st_gid st_size st_mtime_ns st_ctime_ns".split()  # noqa: SIM905 - compact fixed field set
    return tuple(getattr(details, field) for field in fields)


def _bounded_wheel_sha256(path: Path) -> str:
    identity = _bounded_file_identity(
        path,
        maximum=64 << 20,
        executable=False,
        owner_uid=os.getuid() if hasattr(os, "getuid") else None,
        single_link=True,
        close_mode=0o600,
    )
    return str(identity["sha256"])


def _partition_evidence(nodes: Sequence[Any]) -> list[dict[str, Any]]:
    return [
        {
            "nodeid": node.nodeid,
            "invariant_id": node.invariant_id,
            "tier": node.tier,
            "execution_kind": node.execution_kind,
            "max_runtime_s": node.max_runtime_s,
            "scratch_mb": node.scratch_mb,
        }
        for node in nodes
    ]


def _open_evidence_directory(path: Path) -> int:
    before = path.lstat()
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_DIRECTORY", 0)
    if os.name != "nt":
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        current = path.lstat()
        if (
            not stat.S_ISDIR(opened.st_mode)
            or _descriptor_identity(before) != _descriptor_identity(opened)
            or _descriptor_identity(opened) != _descriptor_identity(current)
            or stat.S_IMODE(opened.st_mode) != 0o700
            or (hasattr(os, "getuid") and opened.st_uid != os.getuid())
            or os.listdir(descriptor)
        ):
            raise RuntimeError("evidence directory is not empty owner-private authority")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _require_evidence_directory(path: Path, descriptor: int, names: tuple[str, ...]) -> None:
    opened, current = os.fstat(descriptor), path.lstat()
    if (
        _descriptor_identity(opened) != _descriptor_identity(current)
        or stat.S_IMODE(opened.st_mode) != 0o700
        or tuple(sorted(os.listdir(descriptor))) != names
    ):
        raise RuntimeError("evidence directory identity or contents changed")


def _write_private_json(directory_fd: int, name: str, value: object) -> None:
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(
        name,
        flags,
        0o600,
        dir_fd=directory_fd,
    )
    try:
        remaining = memoryview(payload)
        while remaining:
            written = os.write(descriptor, remaining)
            if written < 1:
                raise OSError("short write while creating gate evidence")
            remaining = remaining[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


_PYTEST_BOOTSTRAP = (
    "import anyio.pytest_plugin,pathlib,pytest,pytest_asyncio.plugin,sys,xdist.plugin;"
    "plugins=(xdist.plugin,pytest_asyncio.plugin,anyio.pytest_plugin);"
    "root=str(pathlib.Path(sys.argv[1]).resolve(strict=True));"
    "site=sys.argv[2];sys.executable=sys.argv[3];"
    "sys.path[:0]=(([site] if site!='-' else [])+[root,str(pathlib.Path(root)/'tests')]);"
    "raise SystemExit(pytest.main(sys.argv[4:],plugins=plugins))"
)
_EXPLICIT_PYTEST_PLUGINS = (
    "-p",
    "no:cacheprovider",
    "-p",
    "tools.quality_gate",
)


def _tier_pytest_command(
    *,
    name: str,
    python: str = sys.executable,
    source: Path = ROOT,
    environment: Mapping[str, str] | None = None,
    report: str | Path | None,
    collection: str | Path,
    selection: str | Path | None = None,
    modules: Sequence[str],
    workers: int,
    distribution: str,
    basetemp: str | Path,
    collect_only: bool = False,
) -> GateCommand:
    parallel = ("-n", "0") if workers == 1 else ("-n", str(workers), f"--dist={distribution}")
    pytest_arguments = (
        "-q",
        *(("-q", "-q") if collect_only else ()),
        "-r",
        "a",
        "-o",
        "addopts=",
        "-o",
        "pythonpath=",
        "-o",
        "tmp_path_retention_policy=failed",
        "--import-mode=importlib",
        *(("--collect-only",) if collect_only else ()),
        *_EXPLICIT_PYTEST_PLUGINS,
        *parallel,
        f"--rootdir={source}",
        f"{_COLLECTION_OPTION}={collection}",
        *((f"{_SELECTION_OPTION}={selection}",) if selection is not None else ()),
        *((f"--junitxml={report}",) if report is not None else ()),
        f"--basetemp={basetemp}",
        *modules,
    )
    command_environment = {} if environment is None else environment
    installed_site = command_environment.get(_INSTALLED_SITE_ENV, "-")
    return GateCommand(
        name,
        (
            python,
            "-I",
            "-B",
            "-c",
            _PYTEST_BOOTSTRAP,
            str(source),
            installed_site,
            python,
            *pytest_arguments,
        ),
        command_environment,
        cwd=source,
        timeout_s=3600,
    )


def _junit_durations(report_path: Path) -> dict[str, int]:
    summary = junit_summary(report_path)
    root = ET.parse(report_path).getroot()
    durations: dict[str, int] = {}
    for testcase in (element for element in root.iter() if _xml_local_name(element.tag) == "testcase"):
        values = [
            prop.attrib.get("value")
            for prop in testcase.iter()
            if _xml_local_name(prop.tag) == "property" and prop.attrib.get("name") == _NODEID_PROPERTY
        ]
        try:
            seconds = Decimal(testcase.attrib["time"])
        except (KeyError, InvalidOperation) as exc:
            raise ValueError("JUnit testcase has an invalid duration") from exc
        if len(values) != 1 or values[0] is None or not seconds.is_finite() or seconds < 0:
            raise ValueError("JUnit testcase has an invalid duration")
        durations[values[0]] = int(seconds * 1_000_000_000)
    if Counter(durations) != Counter(summary.nodeids):
        raise ValueError("JUnit durations do not cover the exact completed selection")
    return durations


def _whitespace_argv(source: Path, base_sha: str, candidate_sha: str) -> tuple[str, ...]:
    return GIT, "-c", _NO_GLOBAL_GIT_ATTRIBUTES, "-C", str(source), "diff", "--check", base_sha, candidate_sha


def _tier_static_commands(
    source: Path,
    *,
    python: str,
    tier: str,
    base_sha: str,
    candidate_sha: str,
    environment: Mapping[str, str],
) -> tuple[GateCommand, ...]:
    git_environment = _git_environment()
    git_environment.update(
        (name, value) for name, value in environment.items() if not name.startswith("GIT_")
    )
    result = [
        GateCommand(
            "committed whitespace errors",
            _whitespace_argv(source, base_sha, candidate_sha),
            git_environment,
            cwd=source,
            timeout_s=120,
        )
    ]
    excluded = {"whitespace errors", *(("quality toolchain",) if tier == "change" else ())}
    result.extend(
        GateCommand(
            command.name,
            command.argv,
            environment,
            cwd=source,
            timeout_s=1800,
        )
        for command in static_commands(python)
        if command.name not in excluded
    )
    return tuple(result)


def _bounded_file_identity(
    path: Path,
    *,
    maximum: int,
    executable: bool,
    owner_uid: int | None = None,
    single_link: bool = False,
    close_mode: int | None = None,
) -> dict[str, int | str]:
    before = path.lstat()
    mode = stat.S_IMODE(before.st_mode)
    if (
        not stat.S_ISREG(before.st_mode)
        or not 0 < before.st_size <= maximum
        or (close_mode is None and mode & 0o022)
        or (executable and not mode & 0o111)
        or (owner_uid is not None and before.st_uid != owner_uid)
        or (single_link and before.st_nlink != 1)
        or (close_mode is not None and (owner_uid is None or close_mode & 0o022))
    ):
        raise RuntimeError("quality-gate input has unsafe metadata")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    if os.name != "nt":
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    digest = hashlib.sha256()
    try:
        opened = os.fstat(descriptor)
        if _stat_identity(opened) != _stat_identity(before):
            raise RuntimeError("quality-gate input changed while opening")
        if close_mode is not None:
            os.fchmod(descriptor, close_mode)
            opened = os.fstat(descriptor)
            if stat.S_IMODE(opened.st_mode) != close_mode or _stat_identity(path.lstat()) != _stat_identity(
                opened
            ):
                raise RuntimeError("quality-gate input changed while closing metadata")
        while chunk := os.read(descriptor, 1024 * 1024):
            digest.update(chunk)
        if _stat_identity(os.fstat(descriptor)) != _stat_identity(opened) or _stat_identity(
            path.lstat()
        ) != _stat_identity(opened):
            raise RuntimeError("quality-gate input changed while hashing")
    finally:
        os.close(descriptor)
    if close_mode is not None:
        mode = close_mode
    return {"sha256": digest.hexdigest(), "size_bytes": before.st_size, "mode": f"{mode:04o}"}


def _host_command(argv: Sequence[str], *, timeout: int = 10) -> str:
    completed = subprocess.run(  # noqa: S603 - fixed exact-host executables and literal argv
        argv,
        executable=argv[0],
        env={"LC_ALL": "C.UTF-8", "PATH": "/usr/bin:/bin"},
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=False,
        timeout=timeout,
    )
    if completed.returncode or len(completed.stdout) + len(completed.stderr) > 8192:
        raise RuntimeError(f"exact-host prerequisite command failed: {Path(argv[0]).name}")
    return completed.stdout.decode("utf-8", errors="strict")


def _dpkg_file_identity(path: Path, package: str, *, executable: bool) -> dict[str, int | str]:
    if path.resolve(strict=True) != path:
        raise RuntimeError("exact-host package path is not canonical")
    identity = _bounded_file_identity(path, maximum=64 << 20, executable=executable, owner_uid=0)
    owners = {
        line.split(": ", 1)[0].split(":", 1)[0]
        for line in _host_command(("/usr/bin/dpkg-query", "-S", str(path))).splitlines()
    }
    fields = (
        _host_command(
            (
                "/usr/bin/dpkg-query",
                "-W",
                "-f=${db:Status-Status}\t${binary:Package}\t${Version}\t${Architecture}\n",
                package,
            )
        )
        .strip()
        .split("\t")
    )
    if (
        owners != {package}
        or len(fields) != 4
        or fields[0] != "installed"
        or fields[1].split(":")[0] != package
    ):
        raise RuntimeError("exact-host package ownership or installed status is invalid")
    if _host_command(("/usr/bin/dpkg", "--verify", package)).strip():
        raise RuntimeError("exact-host package verification reported drift")
    return {**identity, "package": package, "version": fields[2], "architecture": fields[3]}


def _exact_host_evidence() -> dict[str, Any]:
    release = platform.freedesktop_os_release()
    release_identity = release.get("ID"), release.get("VERSION_ID"), platform.machine()
    if release_identity != ("ubuntu", "26.04", "x86_64"):
        raise RuntimeError("exact-release requires the provisioned Ubuntu 26.04 x86_64 host")
    if (
        Path("/sys/module/apparmor/parameters/enabled").read_text().strip() != "Y"
        or Path("/proc/sys/kernel/apparmor_restrict_unprivileged_userns").read_text().strip() != "1"
    ):
        raise RuntimeError("exact-release AppArmor userns hardening is absent")
    dpkg_query = _dpkg_file_identity(Path("/usr/bin/dpkg-query"), "dpkg", executable=True)
    dpkg = _dpkg_file_identity(Path("/usr/bin/dpkg"), "dpkg", executable=True)
    parser = _dpkg_file_identity(Path("/usr/sbin/apparmor_parser"), "apparmor", executable=True)
    policy = _dpkg_file_identity(Path("/etc/apparmor.d/bwrap-userns-restrict"), "apparmor", executable=False)
    bwrap = _dpkg_file_identity(Path("/usr/bin/bwrap"), "bubblewrap", executable=True)
    host_user, host_net = os.readlink("/proc/self/ns/user"), os.readlink("/proc/self/ns/net")
    mounts = (
        ("--ro-bind", "/usr", "/usr")
        + ("--ro-bind-try", "/lib", "/lib", "--ro-bind-try", "/lib64", "/lib64")
        + ("--dir", "/etc", "--ro-bind-try", "/etc/ld.so.cache", "/etc/ld.so.cache")
        + ("--proc", "/proc", "--dev", "/dev")
    )
    probe = "readlink /proc/self/ns/user; readlink /proc/self/ns/net; cat /proc/self/attr/current"
    command = (
        ("/usr/bin/bwrap", "--unshare-all", "--unshare-user")
        + ("--uid", str(os.getuid()), "--gid", str(os.getgid()))
        + ("--cap-drop", "ALL", "--disable-userns", "--die-with-parent", "--new-session")
        + mounts
        + ("--", "/usr/bin/sh", "-c", probe)
    )
    output = _host_command(command).splitlines()
    if (
        len(output) != 3
        or output[0] == host_user
        or output[1] == host_net
        or output[2] != "bwrap//&unpriv_bwrap (enforce)"
    ):
        raise RuntimeError("exact-host bwrap/AppArmor namespace smoke is not enforcing")
    return {
        "os": {"id": "ubuntu", "version": "26.04", "architecture": "x86_64"},
        "dpkg_query": dpkg_query,
        "dpkg": dpkg,
        "apparmor_parser": parser,
        "apparmor_policy": policy,
        "bubblewrap": bwrap,
        "userns_restriction": 1,
        "smoke": {
            "user_namespace_distinct": True,
            "network_namespace_distinct": True,
            "profile_stack": output[2],
        },
    }


def _write_tier_summary(directory_fd: int, summary: Mapping[str, Any]) -> None:
    host = summary.get("release_host_capacity")
    contour = host.get("host_contour") if isinstance(host, Mapping) else None
    if summary.get("tier") == "exact-release" and contour != _exact_host_evidence():
        raise RuntimeError("exact-host contour drifted before evidence publication")
    _write_private_json(directory_fd, "quality-gate-summary.json", summary)


def _backup_observation(directory: Path) -> dict[str, int | str]:
    files = tuple(directory.glob("*.sqlite3"))
    if not 0 < len(files) <= 256:
        raise RuntimeError("nightly backup observation count is outside its bound")
    identities = [_bounded_file_identity(path, maximum=16 << 30, executable=False) for path in files]
    total = sum(int(item["size_bytes"]) for item in identities)
    if total > 64 << 30:
        raise RuntimeError("nightly backup observation bytes exceed their bound")
    members = sorted((item["sha256"], item["size_bytes"]) for item in identities)
    payload = json.dumps(
        {"domain": "friday.quality-gate-backups.v1", "count": len(files), "members": members},
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode()
    return {
        "file_count": len(files),
        "total_bytes": total,
        "aggregate_sha256": hashlib.sha256(payload).hexdigest(),
    }


def _observation_assets(environment: Mapping[str, str], *, powershell_sha256: str) -> dict[str, Any]:
    syncthing = _bounded_file_identity(
        Path(environment["FRIDAY_REAL_SYNCTHING_BINARY"]), maximum=64 << 20, executable=True
    )
    powershell = _bounded_file_identity(
        Path(environment["QUALITY_GATE_POWERSHELL_BINARY"]), maximum=256 << 20, executable=True
    )
    if syncthing["sha256"] != _SYNCTHING_AMD64_SHA256:
        raise RuntimeError("nightly Syncthing binary differs from the reviewed release")
    if powershell["sha256"] != powershell_sha256:
        raise RuntimeError("nightly PowerShell binary differs from the operator-reviewed digest")
    return {
        "schema": "friday.quality-gate-observation-assets.v1",
        "real_backups": _backup_observation(Path(environment["FRIDAY_TEST_BACKUPS_DIR"])),
        "syncthing": syncthing,
        "powershell": powershell,
    }


def _observation_environment(
    environment: Mapping[str, str],
) -> tuple[dict[str, str], dict[str, Any]]:
    result = dict(environment)
    kinds = {
        "QUALITY_GATE_REAL_BACKUPS_DIR": "directory",
        "QUALITY_GATE_SYNCTHING_BINARY": "file",
        "QUALITY_GATE_POWERSHELL_BINARY": "file",
    }
    resolved: dict[str, Path] = {}
    for name, kind in kinds.items():
        raw = os.environ.get(name, "").strip()
        if not raw:
            raise RuntimeError(f"nightly observation asset is absent: {name}")
        path = Path(raw)
        if not path.is_absolute() or path.is_symlink():
            raise RuntimeError(f"nightly observation asset is not canonical: {name}")
        path = path.resolve(strict=True)
        if path != Path(raw):
            raise RuntimeError(f"nightly observation asset is not canonical: {name}")
        if (kind == "directory" and not path.is_dir()) or (
            kind == "file" and (not path.is_file() or not os.access(path, os.X_OK))
        ):
            raise RuntimeError(f"nightly observation asset is unusable: {name}")
        resolved[name] = path
    powershell = resolved["QUALITY_GATE_POWERSHELL_BINARY"]
    path_value = f"{powershell.parent}{os.pathsep}{result.get('PATH', os.defpath)}"
    selected = shutil.which("pwsh", path=path_value) or shutil.which("powershell", path=path_value)
    if (
        powershell.name not in {"pwsh", "powershell"}
        or not selected
        or Path(selected).resolve() != powershell
    ):
        raise RuntimeError("nightly PowerShell binary is not the authenticated PATH selection")
    expected_powershell = os.environ.get("QUALITY_GATE_POWERSHELL_SHA256", "").strip()
    if re.fullmatch(r"[0-9a-f]{64}", expected_powershell) is None:
        raise RuntimeError("nightly PowerShell requires one operator-reviewed SHA-256")
    result.update(
        {
            "FRIDAY_TEST_BACKUPS_DIR": str(resolved["QUALITY_GATE_REAL_BACKUPS_DIR"]),
            "FRIDAY_REAL_SYNCTHING_BINARY": str(resolved["QUALITY_GATE_SYNCTHING_BINARY"]),
            "QUALITY_GATE_SYNCTHING_BINARY": str(resolved["QUALITY_GATE_SYNCTHING_BINARY"]),
            "QUALITY_GATE_POWERSHELL_BINARY": str(powershell),
            "PATH": path_value,
        }
    )
    return result, _observation_assets(result, powershell_sha256=expected_powershell)


def _require_observation_assets_stable(environment: Mapping[str, str], expected: Mapping[str, Any]) -> None:
    actual = _observation_assets(environment, powershell_sha256=str(expected["powershell"]["sha256"]))
    if actual != expected:
        raise RuntimeError("nightly observation assets drifted during execution")


def _build_reusable_wheel(
    source: Path,
    scratch: Path,
    *,
    candidate_sha: str,
    python: str,
    environment: Mapping[str, str],
    runner: Callable[[GateCommand], int],
    comparison_epoch_sha: str | None = None,
    comparison_sha256: str | None = None,
) -> tuple[Path, str, str | None, Path, Path]:
    def build_one(label: str, build_source: Path, epoch_sha: str) -> tuple[Path, str, dict[str, str]]:
        project = scratch / f"{label}-project"
        dist = scratch / f"{label}-dist"
        shutil.copytree(build_source, project, ignore=shutil.ignore_patterns(".git"))
        dist.mkdir(mode=0o700)
        build_environment = dict(environment)
        build_environment["SOURCE_DATE_EPOCH"] = _git_output(
            build_source, "show", "-s", "--format=%ct", epoch_sha
        )
        build = GateCommand(
            f"{label} wheel build",
            (python, "-I", "-m", "build", "--wheel", "--no-isolation", "--outdir", str(dist)),
            build_environment,
            cwd=project,
            timeout_s=1200,
        )
        if runner(build) != 0:
            raise RuntimeError(f"{label} wheel build failed")
        wheels = tuple(dist.glob("*.whl"))
        if len(wheels) != 1 or not wheels[0].is_file():
            raise RuntimeError(f"{label} wheel build did not produce exactly one wheel")
        wheel = wheels[0]
        digest = _bounded_wheel_sha256(wheel)
        verify = GateCommand(
            f"{label} wheel verifier",
            (
                python,
                str(source / "deploy" / "host-control" / "verify_wheel.py"),
                str(wheel),
                digest,
            ),
            build_environment,
            cwd=source,
            timeout_s=300,
        )
        if runner(verify) != 0:
            raise RuntimeError(f"{label} wheel verifier failed")
        return wheel, digest, build_environment

    def install_one(label: str, wheel: Path, build_environment: Mapping[str, str]) -> tuple[Path, Path]:
        runtime = scratch / f"{label}-runtime"
        installed_site = runtime / "site-packages"
        installed_site.mkdir(parents=True, mode=0o700)
        install = GateCommand(
            f"clean-install {label} wheel",
            (
                python,
                "-I",
                "-m",
                "pip",
                "--isolated",
                "install",
                "--no-deps",
                "--no-compile",
                "--target",
                str(installed_site),
                str(wheel),
            ),
            build_environment,
            cwd=scratch,
            timeout_s=600,
        )
        if runner(install) != 0:
            raise RuntimeError(f"{label} wheel clean install failed")
        return installed_site, Path(python)

    wheel, wheel_sha256, build_environment = build_one("candidate", source, candidate_sha)
    installed_site, runtime_python = install_one("candidate", wheel, build_environment)
    comparison_observed: str | None = None
    if comparison_sha256 is not None:
        if comparison_epoch_sha is None:
            raise RuntimeError("comparison wheel requires an authenticated epoch commit")
        with _candidate_projection(
            comparison_epoch_sha,
            scratch,
            origin=source,
            name="comparison-source",
        ) as comparison_source:
            comparison_wheel, comparison_observed, comparison_environment = build_one(
                "comparison", comparison_source, comparison_epoch_sha
            )
            _require_comparison_wheel(comparison_observed, comparison_sha256)
            installed_site, runtime_python = install_one(
                "comparison", comparison_wheel, comparison_environment
            )
    return wheel, wheel_sha256, comparison_observed, installed_site, runtime_python


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


def _tier_result_identity(tier: str, measurement_only: bool) -> tuple[str, str, bool]:
    if measurement_only:
        return "friday.quality-gate-measurement.v1", "measured", False
    if tier == "exact-release":
        return "friday.quality-gate-summary.v1", "passed", True
    if tier == "nightly":
        return "friday.quality-gate-observation.v1", "observed", False
    return "friday.quality-gate-change.v1", "passed", False


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tier", choices=("change", "exact-release", "nightly"))
    parser.add_argument("--candidate-sha", help="exact candidate commit for a closed tier")
    parser.add_argument("--base-sha", help="strict candidate ancestor for change coverage")
    parser.add_argument("--evidence-dir", type=Path, help="empty private evidence directory")
    parser.add_argument("--comparison-wheel-sha256", help="comparison wheel SHA-256")
    parser.add_argument("--comparison-wheel-epoch-sha", help="comparison wheel commit")
    parser.add_argument(
        "--inventory-collection",
        type=Path,
        help="write one retained isolated serial collection for inventory maintenance",
    )
    parser.add_argument(
        "--phase",
        action="append",
        choices=("all", *PHASES),
        help="legacy diagnostic phase",
    )
    parser.add_argument("--workers", type=_positive_workers, default=20)
    parser.add_argument(
        "--ui-workers",
        type=_positive_workers,
        default=4,
        help=(
            "UI workers (default: 4, one module remains on one worker); "
            f"explicit overrides may use at most {len(UI_TEST_MODULES)} (one per UI module)"
        ),
    )
    parser.add_argument("--dry-run", action="store_true", help="print diagnostic commands")
    return parser


def _inventory_interpreter_is_isolated() -> bool:
    return bool(sys.flags.isolated and sys.flags.safe_path and sys.dont_write_bytecode)


def execute_inventory_collection(
    args: argparse.Namespace, *, command_runner: Callable[[GateCommand], int] = run_command
) -> int:
    target = args.inventory_collection
    forbidden = (
        args.tier,
        args.candidate_sha,
        args.base_sha,
        args.evidence_dir,
        args.comparison_wheel_sha256,
        args.comparison_wheel_epoch_sha,
        args.phase,
        args.dry_run,
    )
    try:
        if not _inventory_interpreter_is_isolated():
            raise RuntimeError("inventory collection requires an isolated -I -B interpreter")
        if any(value is not None and value is not False for value in forbidden) or (
            args.workers,
            args.ui_workers,
        ) != (20, 4):
            raise RuntimeError("inventory collection does not combine with gate modes")
        if target is None or not target.is_absolute() or target.exists():
            raise RuntimeError("inventory collection target must be an absent absolute path")
        parent = target.parent.resolve(strict=True)
        if not parent.is_dir() or parent == ROOT or parent.is_relative_to(ROOT):
            raise RuntimeError("inventory collection target must be outside the checkout")
        with tempfile.TemporaryDirectory(prefix="fq-inventory-", dir=_scratch_parent()) as raw:
            scratch = Path(raw)
            with _isolated_test_environment(
                scratch, prepare_schema_backups=False, source_root=ROOT
            ) as environment:
                command = _tier_pytest_command(
                    name="authoritative inventory collection",
                    python=sys.executable,
                    source=ROOT,
                    environment=environment,
                    report=None,
                    collection=target,
                    selection=None,
                    modules=("tests",),
                    workers=1,
                    distribution="load",
                    basetemp=scratch / "pytest",
                    collect_only=True,
                )
                if command_runner(command) != 0:
                    return 1
        count = len(collection_nodeids(target))
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return 2
    print(f"Inventory collection: {count} exact nodes")
    return 0


def execute_tier(
    args: argparse.Namespace,
    *,
    command_runner: Callable[[GateCommand], int] | None = None,
) -> int:
    started_ns = time.monotonic_ns()
    started_times = os.times()
    if command_runner is not None:
        print("FAILED: closed tiers do not accept an injected command runner", file=sys.stderr)
        return 2
    runner = run_command
    commit_pattern = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})").fullmatch
    candidate_sha = args.candidate_sha
    base_sha = args.base_sha
    comparison_wheel_sha256 = args.comparison_wheel_sha256
    comparison_wheel_epoch_sha = args.comparison_wheel_epoch_sha
    measurement_only = comparison_wheel_sha256 is not None
    host_capacity: dict[str, Any] | None = None
    evidence_fd = -1
    if args.phase or args.dry_run:
        print("FAILED: closed tiers do not accept legacy phase or dry-run modes", file=sys.stderr)
        return 2
    if args.workers > 24 or args.ui_workers > len(UI_TEST_MODULES):
        print("FAILED: closed tier worker topology is outside its bounded plan", file=sys.stderr)
        return 2
    if args.tier == "exact-release" and (args.workers, args.ui_workers) != (20, 4):
        print("FAILED: exact-release requires the canonical 20/4 worker topology", file=sys.stderr)
        return 2
    if not isinstance(candidate_sha, str) or commit_pattern(candidate_sha) is None:
        print("FAILED: --candidate-sha must be one full lowercase commit", file=sys.stderr)
        return 2
    if not (sys.flags.isolated and sys.flags.safe_path and sys.dont_write_bytecode):
        print("FAILED: closed tiers require an isolated -I -B interpreter", file=sys.stderr)
        return 2
    try:
        if _git_output(ROOT, "rev-parse", "--verify", f"{candidate_sha}^{{commit}}") != candidate_sha:
            raise RuntimeError("candidate commit identity is not exact")
        _require_candidate_launcher(candidate_sha)
        load_inventory = _candidate_inventory_loader()
        if args.tier == "exact-release":
            host_capacity = {**_exact_host_capacity(), "host_contour": _exact_host_evidence()}
        if args.tier != "nightly":
            if not isinstance(base_sha, str) or commit_pattern(base_sha) is None or base_sha == candidate_sha:
                raise RuntimeError("change tiers require a distinct full --base-sha")
            if _git_output(ROOT, "rev-parse", "--verify", f"{base_sha}^{{commit}}") != base_sha:
                raise RuntimeError("base commit identity is not exact")
            _git_output(ROOT, "merge-base", "--is-ancestor", base_sha, candidate_sha)
        elif base_sha is not None:
            raise RuntimeError("nightly does not accept a change interval")
        comparison_requested = comparison_wheel_sha256 is not None or comparison_wheel_epoch_sha is not None
        if comparison_requested:
            if (
                args.tier != "exact-release"
                or re.fullmatch(r"[0-9a-f]{64}", comparison_wheel_sha256 or "") is None
                or commit_pattern(comparison_wheel_epoch_sha or "") is None
            ):
                raise RuntimeError(
                    "comparison wheel requires exact-release, one SHA-256, and its epoch commit"
                )
            assert isinstance(comparison_wheel_epoch_sha, str)
            if (
                _git_output(
                    ROOT,
                    "rev-parse",
                    "--verify",
                    f"{comparison_wheel_epoch_sha}^{{commit}}",
                )
                != comparison_wheel_epoch_sha
            ):
                raise RuntimeError("comparison wheel epoch commit identity is not exact")
            _git_output(
                ROOT,
                "merge-base",
                "--is-ancestor",
                comparison_wheel_epoch_sha,
                candidate_sha,
            )
        evidence_dir = args.evidence_dir
        if evidence_dir is None or not evidence_dir.is_absolute() or evidence_dir.is_symlink():
            raise RuntimeError("a real absolute --evidence-dir is required")
        evidence_dir = evidence_dir.resolve(strict=True)
        if not evidence_dir.is_dir() or any(evidence_dir.iterdir()):
            raise RuntimeError("--evidence-dir must be an existing empty directory")
        if evidence_dir == ROOT or evidence_dir.is_relative_to(ROOT):
            raise RuntimeError("--evidence-dir must be outside the candidate checkout")
        evidence_fd = _open_evidence_directory(evidence_dir)
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
        if evidence_fd >= 0:
            os.close(evidence_fd)
        print(f"FAILED: {exc}", file=sys.stderr)
        return 2

    peak_scratch_bytes = 0
    completed_steps: list[str] = []
    full_nodeids: tuple[str, ...] = ()
    classified: tuple[Any, ...] = ()
    durations: dict[str, int] = {}
    wheel_sha256: str | None = None
    comparison_observed_sha256: str | None = None
    candidate_tree = ""
    scratch_groups: list[dict[str, int | str]] = []
    observed_by_step: dict[str, int] = {}
    effective_workers = {"non_ui": 0, "ui": 0}
    observation_assets: dict[str, Any] | None = None

    try:
        with tempfile.TemporaryDirectory(prefix="fq-", dir=_scratch_parent()) as raw_scratch:
            scratch = Path(raw_scratch)
            scratch.chmod(0o700)

            def measured(
                command: GateCommand,
                *,
                observation_root: Path | None = None,
            ) -> int:
                nonlocal peak_scratch_bytes
                if observation_root is None:
                    returncode = runner(command)
                    observed_bytes = _directory_bytes(scratch)
                else:
                    root = observation_root
                    outside_bytes = (
                        0 if root == scratch else max(0, _directory_bytes(scratch) - _directory_bytes(root))
                    )
                    with _scratch_peak_sampler(root, 0.5) as observed_peak:
                        returncode = runner(command)
                    observed_bytes = observed_peak[0]
                    peak_scratch_bytes = max(peak_scratch_bytes, outside_bytes + observed_bytes)
                observed_by_step[command.name] = observed_bytes
                completed_steps.append(command.name)
                peak_scratch_bytes = max(peak_scratch_bytes, observed_bytes)
                return returncode

            with (
                _scratch_peak_sampler(scratch, 5.0) as aggregate_scratch_peak,
                _candidate_projection(candidate_sha, scratch) as source,
            ):
                peak_scratch_bytes = _directory_bytes(scratch)
                candidate_tree = _git_output(source, "rev-parse", f"{candidate_sha}^{{tree}}")
                inventory = load_inventory(source / "tools" / "quality_gate_inventory.tsv")
                inventory.validate_candidate_modules(source, candidate_sha)
                static_environment = _git_environment()
                static_environment.pop("PYTHONPATH", None)
                static_environment.pop("PYTEST_ADDOPTS", None)
                static_environment.update(
                    {
                        "PYTHONDONTWRITEBYTECODE": "1",
                        "PYTHONPYCACHEPREFIX": str(scratch / "static-cache"),
                        "MYPY_CACHE_DIR": str(scratch / "mypy-cache"),
                        "RUFF_CACHE_DIR": str(scratch / "ruff-cache"),
                    }
                )
                if args.tier != "nightly":
                    assert isinstance(base_sha, str)
                    for command in _tier_static_commands(
                        source,
                        python=sys.executable,
                        tier=args.tier,
                        base_sha=base_sha,
                        candidate_sha=candidate_sha,
                        environment=static_environment,
                    ):
                        if measured(command) != 0:
                            return 1

                with _isolated_test_environment(
                    scratch,
                    prepare_schema_backups=False,
                    source_root=source,
                ) as raw_environment:
                    environment = dict(raw_environment)
                    test_python = sys.executable
                    if args.tier == "nightly":
                        environment, observation_assets = _observation_environment(environment)
                    if args.tier == "exact-release":
                        (
                            _wheel,
                            wheel_sha256,
                            comparison_observed_sha256,
                            installed_site,
                            test_python,
                        ) = _build_reusable_wheel(
                            source,
                            scratch,
                            candidate_sha=candidate_sha,
                            python=sys.executable,
                            environment=environment,
                            runner=measured,
                            comparison_epoch_sha=comparison_wheel_epoch_sha,
                            comparison_sha256=comparison_wheel_sha256,
                        )
                        environment.update(
                            {_INSTALLED_SITE_ENV: str(installed_site), "PYTHONPATH": str(installed_site)}
                        )

                    full_collection = scratch / "all-tests.json"
                    collect = _tier_pytest_command(
                        name="one authoritative candidate collection",
                        python=test_python,
                        source=source,
                        environment=environment,
                        report=None,
                        collection=full_collection,
                        selection=None,
                        modules=("tests",),
                        workers=1,
                        distribution="load",
                        basetemp=scratch / "collect",
                        collect_only=True,
                    )
                    if measured(collect) != 0:
                        return 1
                    full_nodeids = collection_nodeids(full_collection)
                    classified = inventory.classify(full_nodeids)
                    selected_tiers = (
                        {"change", "exact-release"} if args.tier == "exact-release" else {args.tier}
                    )
                    selected = tuple(node for node in classified if node.tier in selected_tiers)
                    if not selected:
                        raise RuntimeError("selected tier is empty")

                    groups = (
                        (
                            "non-UI",
                            tuple(node for node in selected if node.execution_kind != "browser"),
                        ),
                        ("UI", tuple(node for node in selected if node.execution_kind == "browser")),
                    )
                    for label, nodes in groups:
                        if not nodes:
                            continue
                        group_root = scratch / f"run-{label.lower()}"
                        group_root.mkdir(mode=0o700)
                        with _isolated_test_environment(
                            group_root,
                            prepare_schema_backups=False,
                            source_root=source,
                        ) as group_environment:
                            if _INSTALLED_SITE_ENV in environment:
                                group_environment[_INSTALLED_SITE_ENV] = environment[_INSTALLED_SITE_ENV]
                                group_environment["PYTHONPATH"] = environment["PYTHONPATH"]
                            if args.tier == "nightly":
                                group_environment.update(
                                    {name: environment[name] for name in _OBSERVATION_TEST_ENV}
                                )
                            expected = tuple(node.nodeid for node in nodes)
                            selection_path = group_root / "selection.json"
                            collection_path = group_root / "collection.json"
                            report_path = group_root / "results.xml"
                            _write_collection_manifest(str(selection_path), expected)
                            modules = tuple(dict.fromkeys(node.module_path for node in nodes))
                            requested_workers = args.ui_workers if label == "UI" else args.workers
                            workers = min(requested_workers, len(modules))
                            effective_workers["ui" if label == "UI" else "non_ui"] = workers
                            command = _tier_pytest_command(
                                name=f"{args.tier} {label} tests",
                                python=test_python,
                                source=source,
                                environment=group_environment,
                                report=report_path,
                                collection=collection_path,
                                selection=selection_path,
                                modules=modules,
                                workers=workers,
                                distribution="loadscope" if label == "UI" else "load",
                                basetemp=group_root / "pytest",
                            )
                            scratch_baseline = _directory_bytes(scratch)
                            budget_mb = sum(node.scratch_mb for node in nodes)
                            if measured(command, observation_root=scratch) != 0:
                                return 1
                            observed = collection_nodeids(collection_path)
                            if Counter(observed) != Counter(expected) or not _junit_phase_is_clean(
                                report_path,
                                phase=label,
                                expected_nodeids=expected,
                            ):
                                raise RuntimeError(f"{label} execution differs from its classified tier")
                            durations.update(_junit_durations(report_path))
                            peak_total = observed_by_step[command.name]
                            observed_bytes = max(0, peak_total - scratch_baseline)
                            scratch_groups.append(
                                {
                                    "group": label,
                                    "node_count": len(nodes),
                                    "declared_budget_bytes": budget_mb * 1024 * 1024,
                                    "baseline_bytes": scratch_baseline,
                                    "peak_total_bytes": peak_total,
                                    "incremental_peak_bytes": observed_bytes,
                                    "enforced": False,
                                    "method": "sampled regular-file peak minus fixed baseline",
                                }
                            )

                    expected_executed = {node.nodeid for node in selected}
                    if set(durations) != expected_executed:
                        raise RuntimeError("completed results do not equal the selected tier union")
                    by_nodeid = {node.nodeid: node for node in classified}
                    if any(
                        duration > by_nodeid[nodeid].max_runtime_s * 1_000_000_000
                        for nodeid, duration in durations.items()
                    ):
                        raise RuntimeError("a node exceeded its declared maximum runtime")
                    if observation_assets is not None:
                        _require_observation_assets_stable(environment, observation_assets)
                    peak_scratch_bytes = max(peak_scratch_bytes, _directory_bytes(scratch))

                inventory_digest = inventory.digest
            peak_scratch_bytes = max(peak_scratch_bytes, aggregate_scratch_peak[0])

        finished_times = os.times()
        try:
            import resource

            own_usage = resource.getrusage(resource.RUSAGE_SELF)
            child_usage = resource.getrusage(resource.RUSAGE_CHILDREN)
            rss_scale = 1 if sys.platform == "darwin" else 1024
            max_rss_bytes = int(max(own_usage.ru_maxrss, child_usage.ru_maxrss) * rss_scale)
        except ImportError:
            max_rss_bytes = 0
        wall_ns = time.monotonic_ns() - started_ns

        def cpu_ns(name: str) -> int:
            current = getattr(finished_times, name) + getattr(finished_times, f"children_{name}")
            initial = getattr(started_times, name) + getattr(started_times, f"children_{name}")
            return int((current - initial) * 1_000_000_000)

        _require_candidate_launcher(candidate_sha)
        _require_evidence_directory(evidence_dir, evidence_fd, ())
        schema, result, certification_eligible = _tier_result_identity(args.tier, measurement_only)
        summary = {
            "schema": schema,
            "result": result,
            "certification_eligible": certification_eligible,
            "candidate_sha": candidate_sha,
            "candidate_tree": candidate_tree,
            "base_sha": base_sha,
            "tier": args.tier,
            "inventory_sha256": inventory_digest,
            "invariant_identity": "semantic-function+exact-parameter-set",
            "wheel_sha256": wheel_sha256,
            "test_runtime_wheel_sha256": comparison_observed_sha256 or wheel_sha256,
            "comparison_wheel": {
                "epoch_commit": comparison_wheel_epoch_sha,
                "expected_sha256": comparison_wheel_sha256,
                "observed_sha256": comparison_observed_sha256,
            },
            "topology": {
                "requested_non_ui_workers": args.workers,
                "requested_ui_workers": args.ui_workers,
                "effective_non_ui_workers": effective_workers["non_ui"],
                "effective_ui_workers": effective_workers["ui"],
            },
            "release_host_capacity": host_capacity,
            "completed_steps": completed_steps,
            "partition": _partition_evidence(classified),
            "executed": [
                {"nodeid": nodeid, "duration_ns": durations[nodeid]}
                for nodeid in full_nodeids
                if nodeid in durations
            ],
            "scratch_groups": scratch_groups,
            "workload_metrics_before_evidence": {
                "boundary": "after scratch cleanup, before summary composition",
                "wall_ns": wall_ns,
                "user_ns": cpu_ns("user"),
                "sys_ns": cpu_ns("system"),
                "max_rss_bytes": max_rss_bytes,
                "peak_scratch_bytes": peak_scratch_bytes,
                "retry_count": 0,
            },
        }
        if observation_assets is not None:
            summary["observation_assets"] = observation_assets
        _write_tier_summary(evidence_fd, summary)
        os.fsync(evidence_fd)
        _require_evidence_directory(evidence_dir, evidence_fd, ("quality-gate-summary.json",))
    except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return 1
    finally:
        os.close(evidence_fd)

    outcome = "PASS" if certification_eligible else f"{result.upper()} (NON-CERTIFYING)"
    print(f"\nQuality gate ({args.tier}): {outcome}")
    return 0


def execute(
    args: argparse.Namespace,
    *,
    command_runner: Callable[[GateCommand], int] | None = None,
) -> int:
    runner = command_runner or run_command
    phases = selected_phases(args.phase)
    if args.ui_workers > len(UI_TEST_MODULES):
        print(
            f"FAILED: --ui-workers cannot exceed {len(UI_TEST_MODULES)} (one worker per UI module)",
            file=sys.stderr,
        )
        return 2
    static = static_commands()
    for command in (*static[:1], *(static[1:] if "static" in phases else ())):
        if args.dry_run:
            print(f"[{command.name}] {_display_command(command.argv)}")
        elif runner(command) != 0:
            return 1
    dynamic_phases = {"tests", "ui"}.intersection(phases)
    environment_context = (
        _isolated_test_environment() if dynamic_phases and not args.dry_run else nullcontext(None)
    )
    report_context = (
        tempfile.TemporaryDirectory(prefix="fq-", dir=_scratch_parent())
        if dynamic_phases and not args.dry_run
        else nullcontext(None)
    )
    with environment_context as test_environment, report_context as report_directory:
        if dynamic_phases:
            root = Path(str(report_directory)) if report_directory else Path("<temporary>")
            plans = (
                (
                    "tests",
                    "non-UI",
                    ("tests", *(f"--ignore={m}" for m in UI_TEST_MODULES)),
                    args.workers,
                    "load",
                    "n",
                ),
                ("ui", "UI", UI_TEST_MODULES, args.ui_workers, "loadscope", "u"),
            )
            for phase, label, modules, workers, distribution, stem in plans:
                if phase not in phases:
                    continue
                report_path = root / f"{stem}-results.xml"
                collection_path = root / f"{stem}-collection.json"
                command = _tier_pytest_command(
                    name=f"{label} tests",
                    environment=test_environment,
                    report=report_path,
                    collection=collection_path,
                    modules=modules,
                    workers=workers,
                    distribution=distribution,
                    basetemp=root / stem,
                )
                if args.dry_run:
                    print(f"[{command.name}] {_display_command(command.argv)}")
                    continue
                if runner(command) != 0:
                    return 1
                try:
                    selected_nodeids = collection_nodeids(collection_path)
                except ValueError as exc:
                    print(f"FAILED: {exc}", file=sys.stderr)
                    return 1
                if not _junit_phase_is_clean(report_path, phase=label, expected_nodeids=selected_nodeids):
                    return 1
    outcome = "DRY RUN" if args.dry_run else "PASS"
    print(f"\nQuality gate: {outcome}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.inventory_collection is not None:
        return execute_inventory_collection(args)
    return execute_tier(args) if args.tier else execute(args)


if __name__ == "__main__":
    raise SystemExit(main())
