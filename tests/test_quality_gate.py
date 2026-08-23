from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import stat
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import SimpleNamespace

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
        "ui_workers": len(quality_gate.UI_TEST_MODULES),
        "dry_run": False,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def _collection_argument(command: quality_gate.GateCommand) -> Path:
    prefix = quality_gate._COLLECTION_OPTION + "="
    argument = next(value for value in command.argv if value.startswith(prefix))
    return Path(argument.removeprefix(prefix))


def _write_collection(path: Path, nodeids: Sequence[str]) -> None:
    path.write_text(
        json.dumps({"version": 1, "nodeids": list(nodeids)}),
        encoding="utf-8",
    )


def _testcase(nodeid: str, *, outcome: str = "") -> str:
    terminal = f"<{outcome}/>" if outcome else ""
    return (
        '<testcase name="synthetic"><properties>'
        f'<property name="{quality_gate._NODEID_PROPERTY}" value="{nodeid}"/>'
        f"</properties>{terminal}</testcase>"
    )


def _write_junit(path: Path, nodeids: Sequence[str], *, skipped: int = 0) -> None:
    cases = "".join(
        _testcase(nodeid, outcome="skipped" if index < skipped else "")
        for index, nodeid in enumerate(nodeids)
    )
    path.write_text(
        f'<testsuite tests="{len(nodeids)}" failures="0" errors="0" skipped="{skipped}">{cases}</testsuite>',
        encoding="utf-8",
    )


def _write_schema_manifest(directory: Path, fixtures: Mapping[str, bytes]) -> None:
    entries = [
        {"name": name, "sha256": hashlib.sha256(payload).hexdigest()}
        for name, payload in sorted(fixtures.items())
    ]
    manifest = directory / quality_gate._SCHEMA_FIXTURE_MANIFEST
    manifest.write_text(json.dumps({"version": 1, "fixtures": entries}), encoding="utf-8")
    manifest.chmod(0o644)


def test_static_gate_checks_the_current_package_and_high_bandit_only() -> None:
    commands = quality_gate.static_commands(python="python")
    command_by_name = {command.name: command.argv for command in commands}

    assert command_by_name["quality toolchain"] == (
        "python",
        "tools/quality_toolchain_preflight.py",
    )
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
    test_assets = {
        "FRIDAY_REAL_SYNCTHING_BINARY": "/test-assets/syncthing",
        "FRIDAY_SYNCTHING_AMD64_TARBALL": "/test-assets/syncthing.tar.gz",
    }
    for name, value in test_assets.items():
        monkeypatch.setenv(name, value)
    for prefix in quality_gate._RUNTIME_ENV_PREFIXES:
        monkeypatch.setenv(prefix + "SECRET_SENTINEL", "must-not-survive")
        monkeypatch.setenv(prefix + "HOME", "/sentinel/live-home")
        monkeypatch.setenv(prefix + "ENV_FILE", "/sentinel/live.env")
        monkeypatch.setenv(prefix + "DATABASE_PATH", "/sentinel/live.sqlite3")
        monkeypatch.setenv(prefix + "DATABASE_MUST_EXIST", "1")
        for suffix in quality_gate._RUNTIME_PATH_SELECTOR_SUFFIXES:
            monkeypatch.setenv(prefix + suffix, f"/sentinel/{suffix.casefold()}")

    observed_homes: set[Path] = set()
    observed_commands: list[str] = []
    non_ui_nodeid = "tests/test_quality_gate.py::test_non_ui_probe"
    ui_nodeid = "tests/test_admin_ui_activity.py::test_ui_probe"

    def runner(command: quality_gate.GateCommand) -> int:
        observed_commands.append(command.name)
        if command.name == "quality toolchain":
            assert command.environment is None
            return 0
        environment = command.environment
        assert environment is not None
        home = Path(environment["FRIDAY_HOME"])
        env_file = Path(environment["FRIDAY_ENV_FILE"])
        backup_directory = Path(environment["FRIDAY_TEST_BACKUPS_DIR"])
        observed_homes.add(home)
        assert environment["JERICHO_HOME"] == str(home)
        assert environment["JERICHO_ENV_FILE"] == str(env_file)
        assert environment["FRIDAY_DATABASE_PATH"] == ""
        assert environment["JERICHO_DATABASE_PATH"] == ""
        assert environment["FRIDAY_DATABASE_MUST_EXIST"] == "0"
        assert environment["JERICHO_DATABASE_MUST_EXIST"] == "0"
        assert environment["FRIDAY_LLM_ENABLED"] == "0"
        assert environment["FRIDAY_EMBEDDINGS_ENABLED"] == "0"
        assert environment["FRIDAY_WORKERS_ENABLED"] == "0"
        assert environment["FRIDAY_CODE_EXECUTION_ENABLED"] == "0"
        assert environment["PYTHONDONTWRITEBYTECODE"] == "1"
        assert environment["PYTHONHASHSEED"] == "0"
        assert home.is_dir()
        assert env_file.is_file()
        assert env_file.is_relative_to(home)
        assert backup_directory.is_dir()
        assert backup_directory.is_relative_to(home)
        assert len(tuple(backup_directory.glob("schema-*.sqlite3"))) == 26
        if os.name != "nt":
            assert stat.S_IMODE(home.stat().st_mode) == 0o700
            assert stat.S_IMODE(env_file.stat().st_mode) == 0o600
            assert stat.S_IMODE(backup_directory.stat().st_mode) == 0o700
            assert all(
                stat.S_IMODE(path.stat().st_mode) == 0o600
                for path in backup_directory.glob("schema-*.sqlite3")
            )
        for prefix in quality_gate._RUNTIME_ENV_PREFIXES:
            assert prefix + "SECRET_SENTINEL" not in environment
            for suffix in quality_gate._RUNTIME_PATH_SELECTOR_SUFFIXES:
                assert prefix + suffix not in environment
        for source, alias in quality_gate._TEST_ASSET_ENV_ALIASES.items():
            assert source not in environment
            assert environment[alias] == test_assets[source]
        if command.name == "all-tests collection":
            _write_collection(_collection_argument(command), (non_ui_nodeid, ui_nodeid))
        elif command.name == "non-UI tests":
            _write_collection(_collection_argument(command), (non_ui_nodeid,))
            report_argument = next(
                argument for argument in command.argv if argument.startswith("--junitxml=")
            )
            _write_junit(Path(report_argument.partition("=")[2]), (non_ui_nodeid,))
        elif command.name == "UI tests":
            _write_collection(_collection_argument(command), (ui_nodeid,))
            report_argument = next(
                argument for argument in command.argv if argument.startswith("--junitxml=")
            )
            _write_junit(Path(report_argument.partition("=")[2]), (ui_nodeid,))
        return 0

    result = quality_gate.execute(
        _args(phase=["tests", "ui"], workers=1, ui_workers=1),
        command_runner=runner,
        preflight=lambda: True,
    )

    assert result == 0
    assert observed_commands == [
        "quality toolchain",
        "all-tests collection",
        "non-UI tests",
        "UI tests",
    ]
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


def test_clean_workflow_pins_the_zero_skip_toolchain() -> None:
    workflow = (quality_gate.ROOT / ".github" / "workflows" / "quality.yml").read_text(encoding="utf-8")

    assert 'python-version: "3.14.4"' in workflow
    assert 'node-version: "22.23.2"' in workflow
    assert '-e ".[dev,vectors]"' in workflow
    assert "https://www.rarlab.com/rar/rarlinux-x64-720.tar.gz" in workflow
    assert "d3e7fba3272385b1d0255ee332a1e8c1a6779bb5a5ff9d4d8ac2be846e49ca46" in workflow


def test_non_ui_tests_exclude_all_eleven_browser_modules() -> None:
    command = quality_gate.non_ui_command(
        report_path="report.xml",
        collection_path="collection.json",
        workers=12,
        python="python",
    )

    # Число здесь стоит затем, чтобы браузерный модуль нельзя было завести молча
    # и потерять из общего прогона. Двенадцатый добавлен в 0.196.0 вместе со
    # второй осью времени и разведением кратных рёбер.
    assert len(quality_gate.UI_TEST_MODULES) == 12
    assert command.argv[13:16] == ("-n", "12", "--dist=load")
    assert command.argv[6:10] == ("-o", "addopts=", "-p", "no:cacheprovider")
    assert command.argv[10:12] == ("-p", "tools.quality_gate")
    assert "--junitxml=report.xml" in command.argv
    assert f"{quality_gate._COLLECTION_OPTION}=collection.json" in command.argv
    assert {
        argument.removeprefix("--ignore=") for argument in command.argv if argument.startswith("--ignore=")
    } == set(quality_gate.UI_TEST_MODULES)


def test_non_ui_tests_have_a_true_serial_fallback() -> None:
    command = quality_gate.non_ui_command(
        report_path="report.xml",
        collection_path="collection.json",
        workers=1,
        python="python",
    )

    assert command.argv[13:15] == ("-n", "0")
    assert "--dist=load" not in command.argv


def test_ui_module_inventory_cannot_silently_drift() -> None:
    playwright_import_skip = "importorskip(" + '"playwright.sync_api")'
    discovered = {
        path.relative_to(quality_gate.ROOT).as_posix()
        for path in (quality_gate.ROOT / "tests").glob("test_*.py")
        if playwright_import_skip in path.read_text(encoding="utf-8")
    }

    assert discovered == set(quality_gate.UI_TEST_MODULES)


def test_ui_tests_use_one_loadscope_worker_per_module() -> None:
    command = quality_gate.ui_command(
        report_path="report.xml",
        collection_path="collection.json",
        workers=12,
        python="python",
    )

    assert command.argv[12:15] == ("-n", "12", "--dist=loadscope")
    assert command.argv[6:10] == ("-o", "addopts=", "-p", "no:cacheprovider")
    assert command.argv[10:12] == ("-p", "tools.quality_gate")
    assert command.argv[-12:] == quality_gate.UI_TEST_MODULES
    assert "--junitxml=report.xml" in command.argv
    assert f"{quality_gate._COLLECTION_OPTION}=collection.json" in command.argv


def test_ui_tests_have_a_serial_fallback() -> None:
    command = quality_gate.ui_command(
        report_path="report.xml",
        collection_path="collection.json",
        workers=1,
        python="python",
    )

    assert command.argv[12:14] == ("-n", "0")
    assert "--dist=loadscope" not in command.argv


def test_ui_junit_skips_are_counted(tmp_path: Path) -> None:
    report = tmp_path / "ui.xml"
    report.write_text(
        '<testsuites><testsuite tests="2" failures="0" errors="0" skipped="2">'
        f"{_testcase('tests/test_ui.py::test_a', outcome='skipped')}"
        f"{_testcase('tests/test_ui.py::test_b', outcome='skipped')}"
        '</testsuite><testsuite tests="1" failures="0" errors="0" skipped="1">'
        f"{_testcase('tests/test_ui.py::test_c', outcome='skipped')}"
        "</testsuite></testsuites>",
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
    non_ui_nodeid = "tests/test_probe.py::test_non_ui"
    ui_nodeid = "tests/test_admin_ui_activity.py::test_ui"

    def runner(command: quality_gate.GateCommand) -> int:
        if command.name == "quality toolchain":
            return 0
        if command.name == "all-tests collection":
            _write_collection(_collection_argument(command), (non_ui_nodeid, ui_nodeid))
            return 0
        _write_collection(_collection_argument(command), (ui_nodeid,))
        report_argument = next(argument for argument in command.argv if argument.startswith("--junitxml="))
        report = Path(report_argument.partition("=")[2])
        _write_junit(report, (ui_nodeid,), skipped=1)
        return 0

    result = quality_gate.execute(
        _args(phase=["ui"]),
        command_runner=runner,
        preflight=lambda: True,
    )

    assert result == 1
    assert "UI JUnit reports failures=0, errors=0, skipped=1" in capsys.readouterr().err


def test_non_ui_phase_fails_when_junit_reports_a_skip(capsys) -> None:
    non_ui_nodeid = "tests/test_probe.py::test_non_ui"
    ui_nodeid = "tests/test_admin_ui_activity.py::test_ui"

    def runner(command: quality_gate.GateCommand) -> int:
        if command.name == "quality toolchain":
            return 0
        if command.name == "all-tests collection":
            _write_collection(_collection_argument(command), (non_ui_nodeid, ui_nodeid))
            return 0
        _write_collection(_collection_argument(command), (non_ui_nodeid,))
        report_argument = next(argument for argument in command.argv if argument.startswith("--junitxml="))
        report = Path(report_argument.partition("=")[2])
        _write_junit(report, (non_ui_nodeid,), skipped=1)
        return 0

    result = quality_gate.execute(
        _args(phase=["tests"], workers=1),
        command_runner=runner,
    )

    assert result == 1
    assert "non-UI JUnit reports failures=0, errors=0, skipped=1" in capsys.readouterr().err


def test_junit_report_rejects_suite_and_testcase_count_mismatch(tmp_path: Path) -> None:
    report = tmp_path / "mismatch.xml"
    report.write_text(
        '<testsuite tests="2" failures="0" errors="0" skipped="0"><testcase name="only"/></testsuite>',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="reports 2 tests but contains 1 testcase"):
        quality_gate.junit_summary(report)


def test_requested_phases_keep_canonical_order() -> None:
    assert quality_gate.selected_phases(["ui", "static", "ui"]) == ("static", "ui")
    assert quality_gate.selected_phases(["all"]) == quality_gate.PHASES


def test_more_ui_workers_than_modules_is_rejected(capsys) -> None:
    result = quality_gate.execute(_args(phase=["ui"], ui_workers=13))

    assert result == 2
    assert "cannot exceed 12" in capsys.readouterr().err


def test_schema_fixture_manifest_matches_the_exact_repository_set_and_hashes() -> None:
    directory = quality_gate._SCHEMA_FIXTURE_DIRECTORY
    payload = json.loads((directory / quality_gate._SCHEMA_FIXTURE_MANIFEST).read_text(encoding="utf-8"))
    expected = {entry["name"]: entry["sha256"] for entry in payload["fixtures"]}
    observed = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in directory.iterdir()
        if path.name.startswith("schema-") and path.name.endswith(".sqlite3.gz")
    }

    assert payload["version"] == 1
    assert list(expected) == sorted(expected)
    assert observed == expected


def test_unexpected_schema_fixture_is_rejected_before_it_is_opened(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = tmp_path / "schemas"
    directory.mkdir()
    compressed = gzip.compress(b"synthetic sqlite")
    expected_name = "schema-13.sqlite3.gz"
    (directory / expected_name).write_bytes(compressed)
    (directory / expected_name).chmod(0o644)
    _write_schema_manifest(directory, {expected_name: compressed})
    unexpected = directory / "schema-extra.sqlite3.gz"
    unexpected.write_bytes(b"must never be opened")
    unexpected.chmod(0o644)
    home = tmp_path / "home"
    home.mkdir()
    opened: list[str] = []
    real_open = quality_gate.os.open

    def observed_open(path: str | os.PathLike[str], *args: object, **kwargs: object) -> int:
        opened.append(os.fspath(path))
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(quality_gate, "_SCHEMA_FIXTURE_DIRECTORY", directory)
    monkeypatch.setattr(quality_gate.os, "open", observed_open)

    with pytest.raises(RuntimeError, match="unexpected=.*schema-extra"):
        quality_gate._prepare_synthetic_backup_rehearsal(home)

    assert unexpected.name not in opened
    assert expected_name not in opened


@pytest.mark.skipif(not hasattr(os, "link"), reason="hard links unavailable")
def test_hard_linked_schema_fixture_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    directory = tmp_path / "schemas"
    directory.mkdir()
    source = tmp_path / "source.gz"
    compressed = gzip.compress(b"synthetic sqlite")
    source.write_bytes(compressed)
    fixture = directory / "schema-13.sqlite3.gz"
    os.link(source, fixture)
    fixture.chmod(0o644)
    _write_schema_manifest(directory, {fixture.name: compressed})
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(quality_gate, "_SCHEMA_FIXTURE_DIRECTORY", directory)

    with pytest.raises(RuntimeError, match="multiple hard links"):
        quality_gate._prepare_synthetic_backup_rehearsal(home)


def test_schema_fixture_hash_substitution_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = tmp_path / "schemas"
    directory.mkdir()
    fixture = directory / "schema-13.sqlite3.gz"
    fixture.write_bytes(gzip.compress(b"substituted"))
    fixture.chmod(0o644)
    _write_schema_manifest(directory, {fixture.name: gzip.compress(b"expected")})
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(quality_gate, "_SCHEMA_FIXTURE_DIRECTORY", directory)

    with pytest.raises(RuntimeError, match="SHA-256 mismatch"):
        quality_gate._prepare_synthetic_backup_rehearsal(home)


def test_collection_manifest_rejects_duplicate_nodeids(tmp_path: Path) -> None:
    manifest = tmp_path / "collection.json"
    _write_collection(manifest, ("tests/test_a.py::test_one", "tests/test_a.py::test_one"))

    with pytest.raises(ValueError, match="contains duplicates"):
        quality_gate.collection_nodeids(manifest)


def test_collection_skip_is_terminal_and_suppresses_the_manifest(tmp_path: Path) -> None:
    manifest = tmp_path / "collection.json"

    class Config:
        @staticmethod
        def getoption(name: str, default: object = None) -> object:
            if name == quality_gate._COLLECTION_OPTION:
                return str(manifest)
            return default

    session = SimpleNamespace(config=Config(), exitstatus=0)
    original_length = len(quality_gate._COLLECTION_SKIPS)
    try:
        quality_gate.pytest_collectreport(SimpleNamespace(skipped=True, nodeid="tests/test_skip.py"))
        quality_gate.pytest_sessionfinish(session, 0)
    finally:
        del quality_gate._COLLECTION_SKIPS[original_length:]

    assert session.exitstatus == 1
    assert not manifest.exists()


def test_collection_deselection_is_terminal(tmp_path: Path) -> None:
    manifest = tmp_path / "collection.json"

    class Config:
        @staticmethod
        def getoption(name: str, default: object = None) -> object:
            if name == quality_gate._COLLECTION_OPTION:
                return str(manifest)
            return default

    session = SimpleNamespace(config=Config(), exitstatus=0)
    original_length = len(quality_gate._COLLECTION_DESELECTED)
    try:
        quality_gate.pytest_deselected([SimpleNamespace(nodeid="tests/test_hidden.py::test_hidden")])
        quality_gate.pytest_sessionfinish(session, 0)
    finally:
        del quality_gate._COLLECTION_DESELECTED[original_length:]

    assert session.exitstatus == 1


def test_xdist_worker_deselection_is_terminal_in_the_controller(tmp_path: Path) -> None:
    manifest = tmp_path / "collection.json"

    class Config:
        @staticmethod
        def getoption(name: str, default: object = None) -> object:
            if name == quality_gate._COLLECTION_OPTION:
                return str(manifest)
            if name == "numprocesses":
                return 1
            return default

    node = SimpleNamespace(
        gateway=SimpleNamespace(id="unit-test-gw"),
        workeroutput={"friday_collection_problems": {"skipped": 0, "deselected": 1}},
    )
    session = SimpleNamespace(config=Config(), exitstatus=0)
    previous = quality_gate._COLLECTION_PROBLEMS_BY_WORKER.get("unit-test-gw")
    try:
        quality_gate.pytest_testnodedown(node, None)
        quality_gate.pytest_sessionfinish(session, 0)
    finally:
        if previous is None:
            quality_gate._COLLECTION_PROBLEMS_BY_WORKER.pop("unit-test-gw", None)
        else:
            quality_gate._COLLECTION_PROBLEMS_BY_WORKER["unit-test-gw"] = previous

    assert session.exitstatus == 1
    assert not manifest.exists()


def test_xdist_missing_worker_collection_attestation_is_terminal(tmp_path: Path) -> None:
    manifest = tmp_path / "collection.json"

    class Config:
        @staticmethod
        def getoption(name: str, default: object = None) -> object:
            if name == quality_gate._COLLECTION_OPTION:
                return str(manifest)
            if name == "numprocesses":
                return 1
            return default

    worker_id = "unit-test-missing-attestation"
    node = SimpleNamespace(gateway=SimpleNamespace(id=worker_id), workeroutput={})
    session = SimpleNamespace(config=Config(), exitstatus=0)
    previous_collection = quality_gate._COLLECTIONS_BY_WORKER.get(worker_id)
    previous_attestation = quality_gate._COLLECTION_PROBLEMS_BY_WORKER.get(worker_id)
    quality_gate._COLLECTIONS_BY_WORKER[worker_id] = ("tests/test_a.py::test_one",)
    try:
        quality_gate.pytest_testnodedown(node, None)
        quality_gate.pytest_sessionfinish(session, 0)
    finally:
        if previous_collection is None:
            quality_gate._COLLECTIONS_BY_WORKER.pop(worker_id, None)
        else:
            quality_gate._COLLECTIONS_BY_WORKER[worker_id] = previous_collection
        if previous_attestation is None:
            quality_gate._COLLECTION_PROBLEMS_BY_WORKER.pop(worker_id, None)
        else:
            quality_gate._COLLECTION_PROBLEMS_BY_WORKER[worker_id] = previous_attestation

    assert session.exitstatus == 1
    assert not manifest.exists()


def test_junit_report_requires_one_exact_nodeid_property(tmp_path: Path) -> None:
    report = tmp_path / "missing-nodeid.xml"
    report.write_text(
        '<testsuite tests="1" failures="0" errors="0" skipped="0">'
        '<testcase name="coherent-but-unidentified"><properties/></testcase></testsuite>',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="exactly one exact nodeid"):
        quality_gate.junit_summary(report)


def test_junit_report_rejects_duplicate_nodeids(tmp_path: Path) -> None:
    report = tmp_path / "duplicate.xml"
    nodeid = "tests/test_a.py::test_one"
    report.write_text(
        '<testsuite tests="2" failures="0" errors="0" skipped="0">'
        f"{_testcase(nodeid)}{_testcase(nodeid)}</testsuite>",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="duplicate nodeids"):
        quality_gate.junit_summary(report)


def test_junit_report_rejects_contradictory_nested_aggregate(tmp_path: Path) -> None:
    report = tmp_path / "nested.xml"
    report.write_text(
        '<testsuites tests="2" failures="0" errors="0" skipped="0">'
        '<testsuite tests="1" failures="0" errors="0" skipped="0">'
        f"{_testcase('tests/test_a.py::test_one')}"
        "</testsuite></testsuites>",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="contradictory root aggregate"):
        quality_gate.junit_summary(report)


def test_junit_report_rejects_unsupported_nested_suite(tmp_path: Path) -> None:
    report = tmp_path / "nested-suite.xml"
    report.write_text(
        '<testsuites><testsuite tests="1" failures="0" errors="0" skipped="0">'
        '<testsuite tests="1" failures="0" errors="0" skipped="0">'
        f"{_testcase('tests/test_a.py::test_one')}"
        "</testsuite></testsuite></testsuites>",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="unsupported nested test suites"):
        quality_gate.junit_summary(report)


def test_junit_report_rejects_a_deeply_wrapped_nested_suite(tmp_path: Path) -> None:
    report = tmp_path / "deeply-nested-suite.xml"
    report.write_text(
        '<testsuites><testsuite tests="1" failures="0" errors="0" skipped="0">'
        f"{_testcase('tests/test_a.py::test_one')}"
        '<system-out><wrapper><testsuite tests="0" failures="0" errors="0" skipped="0"/>'
        "</wrapper></system-out></testsuite></testsuites>",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="unsupported nested test suites"):
        quality_gate.junit_summary(report)


def test_junit_report_rejects_a_nested_plural_aggregate(tmp_path: Path) -> None:
    report = tmp_path / "nested-plural.xml"
    report.write_text(
        '<testsuite tests="1" failures="0" errors="0" skipped="0">'
        f"{_testcase('tests/test_a.py::test_one')}"
        '<system-out><testsuites tests="99" failures="99" errors="99" skipped="99"/>'
        "</system-out></testsuite>",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="unsupported nested test aggregates"):
        quality_gate.junit_summary(report)


def test_junit_report_rejects_a_nested_hidden_outcome(tmp_path: Path) -> None:
    report = tmp_path / "nested-outcome.xml"
    report.write_text(
        '<testsuite tests="1" failures="0" errors="0" skipped="0">'
        '<testcase name="synthetic"><properties>'
        f'<property name="{quality_gate._NODEID_PROPERTY}" value="tests/test_a.py::test_one"/>'
        "</properties><system-out><failure/></system-out></testcase></testsuite>",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="misplaced testcase outcome"):
        quality_gate.junit_summary(report)


def test_junit_report_rejects_a_suite_level_hidden_outcome(tmp_path: Path) -> None:
    report = tmp_path / "suite-outcome.xml"
    report.write_text(
        '<testsuite tests="1" failures="0" errors="0" skipped="0">'
        f"{_testcase('tests/test_a.py::test_one')}<failure/></testsuite>",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="misplaced testcase outcome"):
        quality_gate.junit_summary(report)


def test_all_collection_partitions_disjointly_and_completely() -> None:
    non_ui = "tests/test_probe.py::test_non_ui"
    ui = "tests/test_admin_ui_activity.py::test_ui"

    assert quality_gate.partition_collection((non_ui, ui)) == ((non_ui,), (ui,))


@pytest.mark.parametrize("workers", [1, 12])
def test_phase_rejects_a_coherent_substituted_collection(
    workers: int,
    capsys: pytest.CaptureFixture[str],
) -> None:
    expected = "tests/test_probe.py::test_expected"
    substituted = "tests/test_probe.py::test_substituted"
    ui = "tests/test_admin_ui_activity.py::test_ui"

    def runner(command: quality_gate.GateCommand) -> int:
        if command.name == "quality toolchain":
            return 0
        if command.name == "all-tests collection":
            _write_collection(_collection_argument(command), (expected, ui))
            return 0
        _write_collection(_collection_argument(command), (substituted,))
        report_argument = next(argument for argument in command.argv if argument.startswith("--junitxml="))
        _write_junit(Path(report_argument.partition("=")[2]), (substituted,))
        return 0

    assert (
        quality_gate.execute(
            _args(phase=["tests"], workers=workers),
            command_runner=runner,
        )
        == 1
    )
    assert "selection differs from the canonical" in capsys.readouterr().err


@pytest.mark.parametrize("workers", [1, 12])
def test_phase_rejects_junit_substitution_after_exact_collection(
    workers: int,
    capsys: pytest.CaptureFixture[str],
) -> None:
    expected = "tests/test_probe.py::test_expected"
    substituted = "tests/test_probe.py::test_substituted"
    ui = "tests/test_admin_ui_activity.py::test_ui"

    def runner(command: quality_gate.GateCommand) -> int:
        if command.name == "quality toolchain":
            return 0
        if command.name == "all-tests collection":
            _write_collection(_collection_argument(command), (expected, ui))
            return 0
        _write_collection(_collection_argument(command), (expected,))
        report_argument = next(argument for argument in command.argv if argument.startswith("--junitxml="))
        _write_junit(Path(report_argument.partition("=")[2]), (substituted,))
        return 0

    assert (
        quality_gate.execute(
            _args(phase=["tests"], workers=workers),
            command_runner=runner,
        )
        == 1
    )
    assert "JUnit nodeids differ" in capsys.readouterr().err


def test_tests_only_phase_cannot_bypass_toolchain_preflight() -> None:
    observed: list[str] = []

    def runner(command: quality_gate.GateCommand) -> int:
        observed.append(command.name)
        return 1

    assert quality_gate.execute(_args(phase=["tests"]), command_runner=runner) == 1
    assert observed == ["quality toolchain"]
