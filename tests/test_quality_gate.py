from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import stat
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools import quality_gate

CANONICAL_GATE_COMMAND = ".venv/bin/python -I -B tools/quality_gate.py"
CANONICAL_GATE_GUIDANCE = (
    quality_gate.ROOT / "README.md",
    quality_gate.ROOT / "docs" / "LIVE_BATTERY_RUNBOOK.md",
    quality_gate.ROOT / "docs" / "RELEASE_CHECKLIST.md",
)


def _args(**overrides: object) -> argparse.Namespace:
    values: dict[str, object] = {
        "tier": None,
        "candidate_sha": None,
        "base_sha": None,
        "evidence_dir": None,
        "comparison_wheel_sha256": None,
        "comparison_wheel_epoch_sha": None,
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


def _nightly_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path, Path]:
    backups = tmp_path / "private-backups"
    backups.mkdir(mode=0o700)
    for name in ("owner-a.sqlite3", "owner-b.sqlite3"):
        path = backups / name
        path.write_bytes(b"private backup body sentinel")
        path.chmod(0o600)
    syncthing, powershell = tmp_path / "syncthing", tmp_path / "pwsh"
    for path, payload in ((syncthing, b"reviewed syncthing"), (powershell, b"reviewed powershell")):
        path.write_bytes(payload)
        path.chmod(0o700)
    monkeypatch.setattr(
        quality_gate, "_SYNCTHING_AMD64_SHA256", hashlib.sha256(syncthing.read_bytes()).hexdigest()
    )
    values = {
        "QUALITY_GATE_REAL_BACKUPS_DIR": backups,
        "QUALITY_GATE_SYNCTHING_BINARY": syncthing,
        "QUALITY_GATE_POWERSHELL_BINARY": powershell,
        "QUALITY_GATE_POWERSHELL_SHA256": hashlib.sha256(powershell.read_bytes()).hexdigest(),
    }
    for name, value in values.items():
        monkeypatch.setenv(name, str(value))
    return backups, syncthing, powershell


def _collection_session(manifest: Path, workers: int = 0) -> SimpleNamespace:
    values = {quality_gate._COLLECTION_OPTION: str(manifest), "numprocesses": workers}
    getoption = lambda name, default=None: values.get(name, default)  # noqa: E731
    return SimpleNamespace(config=SimpleNamespace(getoption=getoption), exitstatus=0)


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


def test_static_gate_checks_the_current_package_and_high_bandit_only(tmp_path: Path) -> None:
    commands = quality_gate.static_commands(python="python")
    command_by_name = {command.name: command.argv for command in commands}

    assert command_by_name["quality toolchain"] == (
        "python",
        "-I",
        "-B",
        "tools/quality_toolchain_preflight.py",
    )
    assert command_by_name["whitespace errors"] == ("git", "diff", "--check")
    package_roots = ("friday", "friday_host_agent", "friday_package_broker")
    deployment_root = "deploy/host-control"
    assert all(root in command_by_name["ruff format"] for root in package_roots)
    assert deployment_root in command_by_name["ruff format"]
    assert all(root in command_by_name["mypy"] for root in package_roots)
    assert deployment_root in command_by_name["mypy"]
    assert command_by_name["bandit (HIGH only)"] == (
        "python",
        "-I",
        "-B",
        "-m",
        "bandit",
        "-r",
        "friday",
        "friday_host_agent",
        "friday_package_broker",
        deployment_root,
        "-q",
        "--severity-level",
        "high",
    )
    for name in ("ruff lint", "ruff format", "mypy"):
        assert command_by_name[name][:4] == ("python", "-I", "-B", "-m")
    marker = tmp_path / "shadowed"
    shadow = tmp_path / "ruff"
    shadow.mkdir()
    (shadow / "__main__.py").write_text(f"open({str(marker)!r}, 'w').close()", encoding="utf-8")
    subprocess.run((sys.executable, *command_by_name["ruff lint"][1:]), cwd=tmp_path, check=False)
    assert not marker.exists()
    shell_paths = {
        "host-control installer shell syntax": "deploy/host-control/install.sh",
        "host-control uninstaller shell syntax": "deploy/host-control/uninstall.sh",
        "engineer AppArmor installer shell syntax": "deploy/engineer-mode/install-apparmor.sh",
        "engineer AppArmor uninstaller shell syntax": "deploy/engineer-mode/uninstall-apparmor.sh",
        "engineer runtime verifier shell syntax": "deploy/engineer-mode/verify-runtime.sh",
    }
    assert all(command_by_name[name] == ("/bin/sh", "-n", path) for name, path in shell_paths.items())
    assert command_by_name["admin JavaScript syntax"] == (
        "node",
        "--check",
        "friday/admin_ui/static/app.js",
    )
    assert all(
        Path(argument).is_absolute() or Path(argument).parts[:1] != ("jericho",)
        for command in commands
        for argument in command.argv
    )
    assert all(command.environment is None for command in commands)


def test_run_command_passes_an_explicit_environment_to_the_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class Process:
        pid = 2**30

        def wait(self, *, timeout: int) -> int:
            captured["timeout"] = timeout
            return 0

    def fake_popen(argv: tuple[str, ...], **kwargs: object) -> Process:
        captured["argv"] = argv
        captured.update(kwargs)
        return Process()

    monkeypatch.setattr(quality_gate.subprocess, "Popen", fake_popen)
    command = quality_gate.GateCommand("probe", ("python", "-V"), {"ONLY": "scratch"})

    assert quality_gate.run_command(command) == 0
    assert captured["env"] == {"ONLY": "scratch"}
    assert captured["cwd"] == quality_gate.ROOT
    assert captured["start_new_session"] is (os.name != "nt")
    assert captured["timeout"] == command.timeout_s


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group boundary")
def test_timeout_kills_the_whole_group_after_the_leader_exits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    signals: list[int] = []

    class Process:
        pid = 424242
        waits = 0

        def wait(self, *, timeout: int) -> int:
            self.waits += 1
            if self.waits == 1:
                raise subprocess.TimeoutExpired(("probe",), timeout)
            return 0

    def killpg(_pid: int, signal_number: int) -> None:
        signals.append(signal_number)
        if signal_number == 0:
            raise ProcessLookupError

    monkeypatch.setattr(quality_gate.subprocess, "Popen", lambda *_args, **_kwargs: Process())
    monkeypatch.setattr(quality_gate.os, "killpg", killpg)

    assert quality_gate.run_command(quality_gate.GateCommand("probe", ("probe",))) == 124
    assert signals == [signal.SIGTERM, signal.SIGKILL, 0]


def test_pytest_phases_share_one_private_non_live_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture_manifest = json.loads(
        (quality_gate._SCHEMA_FIXTURE_DIRECTORY / quality_gate._SCHEMA_FIXTURE_MANIFEST).read_text(
            encoding="utf-8"
        )
    )
    expected_schema_fixture_count = len(fixture_manifest["fixtures"])
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
    observed_basetemps: set[Path] = set()
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
        basetemp_argument = next(argument for argument in command.argv if argument.startswith("--basetemp="))
        basetemp = Path(basetemp_argument.partition("=")[2])
        observed_basetemps.add(basetemp)
        assert basetemp.parent.is_dir()
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
        assert len(tuple(backup_directory.glob("schema-*.sqlite3"))) == expected_schema_fixture_count
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
        if command.name == "non-UI tests":
            selected = (non_ui_nodeid,)
            _write_collection(_collection_argument(command), selected)
            report_argument = next(
                argument for argument in command.argv if argument.startswith("--junitxml=")
            )
            _write_junit(Path(report_argument.partition("=")[2]), selected)
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
    )

    assert result == 0
    assert observed_commands == [
        "quality toolchain",
        "non-UI tests",
        "UI tests",
    ]
    assert len(observed_homes) == 1
    assert all(not home.exists() for home in observed_homes)
    assert {path.name for path in observed_basetemps} == {"n", "u"}
    assert all(not path.parent.exists() for path in observed_basetemps)


def test_eager_settings_import_derives_the_database_from_the_scratch_home(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
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
            "import friday.config as config; "
            "site=os.environ.get('FRIDAY_QUALITY_GATE_INSTALLED_SITE'); "
            "assert not site or Path(config.__file__).resolve().is_relative_to(Path(site)); "
            "load_settings=config.load_settings; "
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

    site = tmp_path / "wheel-site"
    source = tmp_path / "source"
    origins: dict[str, Path] = {}
    for root in quality_gate._WHEEL_NAMESPACES:
        packaged = site / root / "__init__.py"
        packaged.parent.mkdir(parents=True)
        packaged.touch()
        origins[root] = packaged
    escaped = source / "friday_package_broker" / "__init__.py"
    escaped.parent.mkdir(parents=True)
    escaped.touch()
    origins["friday_package_broker"] = escaped
    monkeypatch.setattr(
        quality_gate.importlib.util,
        "find_spec",
        lambda name: SimpleNamespace(origin=str(origins[name])),
    )
    with pytest.raises(RuntimeError, match="friday_package_broker is not imported"):
        quality_gate._require_installed_wheel_imports(site, {})


def test_canonical_guidance_delegates_to_the_canonical_gate() -> None:
    copied_internal_commands = (
        ".venv/bin/ruff ",
        ".venv/bin/mypy ",
        ".venv/bin/bandit ",
        ".venv/bin/python -m pytest",
        "node --check friday/",
    )

    for path in CANONICAL_GATE_GUIDANCE:
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
    assert "runs-on: ubuntu-24.04" in workflow and "--tier change" in workflow
    assert "cancel-in-progress: false" in workflow and "fetch-depth: 0" in workflow
    assert "$(nproc) >= 4" in workflow and "--workers 4" in workflow and "--ui-workers 1" in workflow
    assert "8 * 1024 * 1024 * 1024" in workflow
    assert all(
        value not in workflow
        for value in (
            "schedule:",
            "exact-release",
            "nightly",
            "self-hosted",
            "QUALITY_GATE_REAL_BACKUPS_DIR",
            "friday-quality-gate-bwrap",
            "/usr/bin/nmap",
            "rarlinux",
            "Syncthing",
        )
    )


def test_ui_module_inventory_cannot_silently_drift() -> None:
    playwright_import_skip = "importorskip(" + '"playwright.sync_api")'
    discovered = {
        path.relative_to(quality_gate.ROOT).as_posix()
        for path in (quality_gate.ROOT / "tests").glob("test_*.py")
        if playwright_import_skip in path.read_text(encoding="utf-8")
    }

    assert discovered == set(quality_gate.UI_TEST_MODULES)


def test_closed_gate_defaults_use_bounded_ui_parallelism(monkeypatch: pytest.MonkeyPatch) -> None:
    args = quality_gate.build_parser().parse_args([])

    assert (args.workers, args.ui_workers) == (20, 4)
    common = {
        "name": "probe",
        "python": "python",
        "source": quality_gate.ROOT,
        "environment": {},
        "report": None,
        "collection": quality_gate.ROOT / "collection.json",
        "selection": None,
        "modules": ("tests",),
        "workers": 1,
        "distribution": "load",
        "basetemp": quality_gate.ROOT / "tmp",
    }
    collection = quality_gate._tier_pytest_command(**common, collect_only=True)
    execution = quality_gate._tier_pytest_command(**common)
    assert "--collect-only" in collection.argv
    assert "--collect-only" not in execution.argv
    ui = quality_gate._tier_pytest_command(
        **{**common, "modules": quality_gate.UI_TEST_MODULES, "workers": 4, "distribution": "loadscope"}
    )
    assert ui.argv[ui.argv.index("-n") : ui.argv.index("-n") + 3] == ("-n", "4", "--dist=loadscope")
    assert ui.argv[-12:] == quality_gate.UI_TEST_MODULES
    monkeypatch.setattr(quality_gate.os, "process_cpu_count", lambda: 24, raising=False)
    monkeypatch.setattr(
        quality_gate.shutil,
        "disk_usage",
        lambda _path: SimpleNamespace(free=quality_gate._EXACT_SCRATCH_FREE_FLOOR),
    )
    assert quality_gate._exact_host_capacity()["effective_cpus"] == 24
    monkeypatch.setattr(quality_gate.os, "process_cpu_count", lambda: 23)
    with pytest.raises(RuntimeError, match="lacks the required CPU"):
        quality_gate._exact_host_capacity()


def test_pytest_bootstrap_preloads_runner_authority_before_candidate_root(tmp_path: Path) -> None:
    for index, relative in enumerate(
        ("pytest.py", "xdist/__init__.py", "pytest_asyncio/__init__.py", "anyio/__init__.py")
    ):
        source = tmp_path / str(index)
        shadow = source / relative
        shadow.parent.mkdir(parents=True)
        shadow.write_text("raise RuntimeError('candidate runner shadow imported')\n", encoding="utf-8")
        completed = subprocess.run(
            (
                sys.executable,
                "-I",
                "-B",
                "-c",
                quality_gate._PYTEST_BOOTSTRAP,
                str(source),
                "-",
                sys.executable,
                "--version",
            ),
            env={**os.environ, "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
            capture_output=True,
            text=True,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr

    source = tmp_path / "candidate"
    site = tmp_path / "installed"
    for root in quality_gate._WHEEL_NAMESPACES:
        packaged = site / root / "__init__.py"
        packaged.parent.mkdir(parents=True)
        packaged.write_text("INSTALLED = True\n", encoding="ascii")
        shadow = source / root / "__init__.py"
        shadow.parent.mkdir(parents=True)
        shadow.write_text("raise RuntimeError('candidate package shadow imported')\n", encoding="ascii")
    completed = subprocess.run(
        (
            sys.executable,
            "-I",
            "-B",
            "-c",
            quality_gate._PYTEST_BOOTSTRAP,
            str(source),
            str(site),
            sys.executable,
            "--version",
        ),
        env={**os.environ, "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert quality_gate._wheel_worker_pythonpath(site, source).split(os.pathsep) == [
        str(site),
        str(source),
    ]

    plugin = source / "tools" / "probe.py"
    plugin.parent.mkdir()
    (plugin.parent / "__init__.py").write_text("", encoding="ascii")
    plugin.write_text("", encoding="ascii")
    probe = source / "tests" / "test_probe.py"
    probe.parent.mkdir()
    probe.write_text(
        "import friday, friday_host_agent, friday_package_broker\n"
        "def test_installed_roots():\n"
        " assert friday.INSTALLED and friday_host_agent.INSTALLED "
        "and friday_package_broker.INSTALLED\n",
        encoding="ascii",
    )
    completed = subprocess.run(
        (
            sys.executable,
            "-I",
            "-B",
            "-c",
            quality_gate._PYTEST_BOOTSTRAP,
            str(source),
            str(site),
            sys.executable,
            "-q",
            "-o",
            "addopts=",
            "-p",
            "no:cacheprovider",
            "-p",
            "xdist.plugin",
            "-p",
            "tools.probe",
            "-n",
            "2",
            str(probe),
        ),
        env={**os.environ, "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_nightly_asset_identity_is_body_free_rename_stable_and_drift_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backups, _syncthing, _powershell = _nightly_fixture(tmp_path, monkeypatch)
    environment, evidence = quality_gate._observation_environment({"PATH": "/usr/bin"})
    assert set(evidence) == {"schema", "real_backups", "syncthing", "powershell"}
    assert evidence["real_backups"]["file_count"] == 2
    serialized = json.dumps(evidence, sort_keys=True)
    assert all(value not in serialized for value in (str(tmp_path), "owner-a", "private backup body"))
    (backups / "owner-a.sqlite3").rename(backups / "renamed.sqlite3")
    quality_gate._require_observation_assets_stable(environment, evidence)
    (backups / "renamed.sqlite3").write_bytes(b"drifted private body")
    with pytest.raises(RuntimeError, match="drifted"):
        quality_gate._require_observation_assets_stable(environment, evidence)


@pytest.mark.parametrize(
    "mutation",
    ("missing", "relative", "symlink", "nonexec", "writable", "oversized", "syncthing", "powershell"),
)
def test_nightly_asset_validation_fails_closed(
    mutation: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backups, syncthing, powershell = _nightly_fixture(tmp_path, monkeypatch)
    if mutation == "missing":
        monkeypatch.delenv("QUALITY_GATE_REAL_BACKUPS_DIR")
    elif mutation == "relative":
        monkeypatch.setenv("QUALITY_GATE_REAL_BACKUPS_DIR", "private-backups")
    elif mutation == "symlink":
        alias = tmp_path / "syncthing-alias"
        alias.symlink_to(syncthing)
        monkeypatch.setenv("QUALITY_GATE_SYNCTHING_BINARY", str(alias))
    elif mutation == "nonexec":
        powershell.chmod(0o600)
    elif mutation == "writable":
        powershell.chmod(0o722)
    elif mutation == "oversized":
        with powershell.open("wb") as stream:
            stream.truncate(256 * 1024 * 1024 + 1)
    elif mutation == "syncthing":
        syncthing.write_bytes(b"substituted")
    else:
        monkeypatch.setenv("QUALITY_GATE_POWERSHELL_SHA256", "0" * 64)
    with pytest.raises((OSError, RuntimeError)):
        quality_gate._observation_environment({"PATH": "/usr/bin"})


def test_exact_host_evidence_binds_package_owned_bwrap_apparmor_contour(monkeypatch) -> None:
    evidence = quality_gate._exact_host_evidence()
    assert evidence["os"] == {"id": "ubuntu", "version": "26.04", "architecture": "x86_64"}
    assert evidence["userns_restriction"] == 1
    for name in ("dpkg_query", "dpkg", "apparmor_parser", "apparmor_policy", "bubblewrap"):
        assert set(evidence[name]) == {"sha256", "size_bytes", "mode", "package", "version", "architecture"}
        assert len(evidence[name]["sha256"]) == 64
    assert evidence["smoke"] == {
        "user_namespace_distinct": True,
        "network_namespace_distinct": True,
        "profile_stack": "bwrap//&unpriv_bwrap (enforce)",
    }
    assert all(path not in json.dumps(evidence, sort_keys=True) for path in ("/etc/", "/usr/"))
    writes: list[object] = []
    monkeypatch.setattr(quality_gate, "_write_private_json", lambda *args: writes.append(args))
    summary = {"tier": "exact-release", "release_host_capacity": {"host_contour": evidence}}
    quality_gate._write_tier_summary(-1, summary)
    assert len(writes) == 1
    monkeypatch.setattr(quality_gate, "_exact_host_evidence", lambda: {"changed": True})
    with pytest.raises(RuntimeError, match="drifted before evidence publication"):
        quality_gate._write_tier_summary(-1, summary)
    assert len(writes) == 1


def test_dry_run_never_executes_commands(capsys) -> None:
    executed = False

    def runner(_command: quality_gate.GateCommand) -> int:
        nonlocal executed
        executed = True
        return 0

    result = quality_gate.execute(
        _args(dry_run=True),
        command_runner=runner,
    )

    assert result == 0
    assert executed is False
    output = capsys.readouterr().out
    assert "--dist=loadscope" in output
    assert "Quality gate: DRY RUN" in output


@pytest.mark.parametrize(("phase", "label"), (("ui", "UI"), ("tests", "non-UI")))
def test_phase_fails_when_junit_reports_a_skip(phase: str, label: str, capsys) -> None:
    non_ui_nodeid = "tests/test_probe.py::test_non_ui"
    ui_nodeid = "tests/test_admin_ui_activity.py::test_ui"

    def runner(command: quality_gate.GateCommand) -> int:
        if command.name == "quality toolchain":
            return 0
        selected = (ui_nodeid if phase == "ui" else non_ui_nodeid,)
        _write_collection(_collection_argument(command), selected)
        report_argument = next(argument for argument in command.argv if argument.startswith("--junitxml="))
        report = Path(report_argument.partition("=")[2])
        _write_junit(report, selected, skipped=1)
        return 0

    result = quality_gate.execute(
        _args(phase=[phase], workers=1),
        command_runner=runner,
    )

    assert result == 1
    assert f"{label} JUnit reports failures=0, errors=0, skipped=1" in capsys.readouterr().err


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
    legacy = quality_gate.execute(_args(phase=["ui"], ui_workers=13))
    tier_ui = quality_gate.execute_tier(_args(tier="change", ui_workers=13))
    tier_non_ui = quality_gate.execute_tier(_args(tier="change", workers=25))
    tier_exact = quality_gate.execute_tier(_args(tier="exact-release", workers=19, ui_workers=4))

    assert (legacy, tier_ui, tier_non_ui, tier_exact) == (2, 2, 2, 2)
    stderr = capsys.readouterr().err
    assert "cannot exceed 12" in stderr
    assert stderr.count("outside its bounded plan") == 2
    assert "canonical 20/4 worker topology" in stderr


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


def test_collection_manifest_rejects_duplicate_nodeids(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = tmp_path / "collection.json"
    _write_collection(manifest, ("tests/test_a.py::test_one", "tests/test_a.py::test_one"))

    with pytest.raises(ValueError, match="contains duplicates"):
        quality_gate.collection_nodeids(manifest)

    oversized = tmp_path / "oversized.json"
    with monkeypatch.context() as bounds:
        bounds.setattr(quality_gate, "_MAX_COLLECTION_BYTES", 32)
        with pytest.raises(ValueError, match="oversized"):
            quality_gate._write_collection_manifest(str(oversized), ("tests/test_a.py::test_one",))
        oversized.write_bytes(b"x" * 33)
        with pytest.raises(ValueError, match="oversized"):
            quality_gate.collection_nodeids(oversized)

    target = tmp_path / "authoritative.json"
    args = quality_gate.build_parser().parse_args(["--inventory-collection", str(target)])
    observed: list[quality_gate.GateCommand] = []

    def runner(command: quality_gate.GateCommand) -> int:
        observed.append(command)
        _write_collection(Path(_collection_argument(command)), ("tests/test_a.py::test_one",))
        return 0

    monkeypatch.setattr(quality_gate, "_inventory_interpreter_is_isolated", lambda: False)
    assert quality_gate.execute_inventory_collection(args, command_runner=runner) == 2
    assert not target.exists() and not observed

    monkeypatch.setattr(quality_gate, "_inventory_interpreter_is_isolated", lambda: True)
    assert quality_gate.execute_inventory_collection(args, command_runner=runner) == 0
    assert quality_gate.collection_nodeids(target) == ("tests/test_a.py::test_one",)
    assert len(observed) == 1 and "--collect-only" in observed[0].argv
    assert "-I" in observed[0].argv and "-B" in observed[0].argv
    assert observed[0].argv[observed[0].argv.index("-n") + 1] == "0"


@pytest.mark.parametrize("problem", ["skip", "deselect"])
def test_local_collection_problem_is_terminal(tmp_path: Path, problem: str) -> None:
    manifest = tmp_path / "collection.json"
    session = _collection_session(manifest)
    skip_length = len(quality_gate._COLLECTION_SKIPS)
    deselected_length = len(quality_gate._COLLECTION_DESELECTED)
    try:
        if problem == "skip":
            quality_gate.pytest_collectreport(SimpleNamespace(skipped=True, nodeid="tests/test_skip.py"))
        else:
            quality_gate.pytest_deselected([SimpleNamespace(nodeid="tests/test_hidden.py::test_hidden")])
        quality_gate.pytest_sessionfinish(session, 0)
    finally:
        del quality_gate._COLLECTION_SKIPS[skip_length:]
        del quality_gate._COLLECTION_DESELECTED[deselected_length:]

    assert session.exitstatus == 1
    assert not manifest.exists()


@pytest.mark.parametrize("problem", ["deselected", "missing-attestation"])
def test_xdist_worker_problem_is_terminal_in_the_controller(tmp_path: Path, problem: str) -> None:
    manifest = tmp_path / "collection.json"
    worker_id = "unit-test-worker-problem"
    node = SimpleNamespace(
        gateway=SimpleNamespace(id=worker_id),
        workeroutput=(
            {"friday_collection_problems": {"skipped": 0, "deselected": 1}} if problem == "deselected" else {}
        ),
    )
    session = _collection_session(manifest, 1)
    previous_collection = quality_gate._COLLECTIONS_BY_WORKER.get(worker_id)
    previous_attestation = quality_gate._COLLECTION_PROBLEMS_BY_WORKER.get(worker_id)
    if problem == "missing-attestation":
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


@pytest.mark.parametrize(
    ("body", "message"),
    (
        (
            '<testsuite tests="1" failures="0" errors="0" skipped="0">'
            '<testcase name="unidentified"><properties/></testcase></testsuite>',
            "exactly one exact nodeid",
        ),
        (
            '<testsuite tests="2" failures="0" errors="0" skipped="0">'
            f"{_testcase('tests/test_a.py::test_one') * 2}</testsuite>",
            "duplicate nodeids",
        ),
        (
            '<testsuites tests="2" failures="0" errors="0" skipped="0">'
            '<testsuite tests="1" failures="0" errors="0" skipped="0">'
            f"{_testcase('tests/test_a.py::test_one')}</testsuite></testsuites>",
            "contradictory root aggregate",
        ),
        (
            '<testsuites><testsuite tests="1" failures="0" errors="0" skipped="0">'
            '<testsuite tests="1" failures="0" errors="0" skipped="0">'
            f"{_testcase('tests/test_a.py::test_one')}</testsuite></testsuite></testsuites>",
            "unsupported nested test suites",
        ),
        (
            '<testsuites><testsuite tests="1" failures="0" errors="0" skipped="0">'
            f"{_testcase('tests/test_a.py::test_one')}<system-out><wrapper>"
            '<testsuite tests="0" failures="0" errors="0" skipped="0"/>'
            "</wrapper></system-out></testsuite></testsuites>",
            "unsupported nested test suites",
        ),
        (
            '<testsuite tests="1" failures="0" errors="0" skipped="0">'
            f"{_testcase('tests/test_a.py::test_one')}<system-out>"
            '<testsuites tests="99" failures="99" errors="99" skipped="99"/>'
            "</system-out></testsuite>",
            "unsupported nested test aggregates",
        ),
        (
            '<testsuite tests="1" failures="0" errors="0" skipped="0">'
            '<testcase name="synthetic"><properties>'
            f'<property name="{quality_gate._NODEID_PROPERTY}" value="tests/test_a.py::test_one"/>'
            "</properties><system-out><failure/></system-out></testcase></testsuite>",
            "misplaced testcase outcome",
        ),
        (
            '<testsuite tests="1" failures="0" errors="0" skipped="0">'
            f"{_testcase('tests/test_a.py::test_one')}<failure/></testsuite>",
            "misplaced testcase outcome",
        ),
    ),
)
def test_junit_report_rejects_untrusted_structure(tmp_path: Path, body: str, message: str) -> None:
    report = tmp_path / "untrusted.xml"
    report.write_text(body, encoding="utf-8")
    with pytest.raises(ValueError, match=message):
        quality_gate.junit_summary(report)


@pytest.mark.parametrize("workers", [1, 12])
def test_phase_rejects_junit_substitution(
    workers: int,
    capsys: pytest.CaptureFixture[str],
) -> None:
    expected = "tests/test_probe.py::test_expected"
    substituted = "tests/test_probe.py::test_substituted"

    def runner(command: quality_gate.GateCommand) -> int:
        if command.name == "quality toolchain":
            return 0
        selected = (expected,)
        _write_collection(_collection_argument(command), selected)
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


@pytest.mark.parametrize("mutation", ["tracked", "untracked", "ignored", "config", "ref", "assume", "mode"])
def test_candidate_projection_is_independent_and_rechecks_every_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    controller_root = quality_gate.ROOT
    origin = tmp_path / "origin"
    origin.mkdir()

    def git(*arguments: str, cwd: Path = origin, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            (quality_gate.GIT, "-C", str(cwd), *arguments),
            check=check,
            text=True,
            capture_output=True,
        )

    git("init", "-q")
    (origin / ".gitignore").write_text(".cache/\n", encoding="utf-8")
    fixture = origin / "fixture"
    fixture.write_text("exact\n", encoding="utf-8")
    tools = origin / "tools"
    tools.mkdir()
    launcher = tools / "quality_gate.py"
    inventory = tools / "quality_gate_inventory.py"
    launcher.write_text("# exact launcher\n", encoding="utf-8")
    inventory.write_text("# exact inventory\n", encoding="utf-8")
    git("add", ".")
    git("-c", "user.name=Gate", "-c", "user.email=gate@example.invalid", "commit", "-qm", "base")
    candidate = git("rev-parse", "HEAD").stdout.strip()
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(quality_gate, "ROOT", origin)
    monkeypatch.setattr(quality_gate, "__file__", str(launcher))

    if mutation == "tracked":
        quality_gate._require_candidate_launcher(candidate)
        for path, exact in (
            (launcher, "# exact launcher\n"),
            (inventory, "# exact inventory\n"),
        ):
            path.write_text("# dirty\n", encoding="utf-8")
            with pytest.raises(RuntimeError, match="launcher is not the clean candidate"):
                quality_gate._require_candidate_launcher(candidate)
            path.write_text(exact, encoding="utf-8")

    with (
        pytest.raises(RuntimeError, match="projection changed"),
        quality_gate._candidate_projection(candidate, scratch) as source,
    ):
        target = source / "fixture"
        if mutation == "tracked":
            target.write_text("changed\n", encoding="utf-8")
        elif mutation == "untracked":
            (source / "residue").touch()
        elif mutation == "ignored":
            (source / ".cache").mkdir()
            (source / ".cache" / "residue").touch()
        elif mutation == "config":
            git("config", "p0g.escape", "true", cwd=source)
        elif mutation == "ref":
            git("update-ref", "refs/heads/escape", "HEAD", cwd=source)
        elif mutation == "assume":
            git("update-index", "--assume-unchanged", "fixture", cwd=source)
            target.write_text("hidden\n", encoding="utf-8")
        else:
            target.chmod(0o755)

    assert git("config", "--get", "p0g.escape", check=False).returncode == 1
    assert git("show-ref", "--quiet", "--verify", "refs/heads/escape", check=False).returncode == 1
    if mutation == "tracked":
        launcher.write_bytes((controller_root / "tools/quality_gate.py").read_bytes())
        inventory.write_bytes((controller_root / "tools/quality_gate_inventory.py").read_bytes())
        git("add", ".")
        git("-c", "user.name=Gate", "-c", "user.email=gate@example.invalid", "commit", "-qm", "controller")
        direct_candidate = git("rev-parse", "HEAD").stdout.strip()
        evidence = tmp_path / "evidence"
        evidence.mkdir(mode=0o700)
        environment = quality_gate._git_environment()
        environment.pop("PYTHONPATH", None)
        direct = subprocess.run(
            [
                sys.executable,
                "-I",
                "-B",
                "tools/quality_gate.py",
                "--tier",
                "change",
                "--candidate-sha",
                direct_candidate,
                "--base-sha",
                candidate,
                "--evidence-dir",
                str(evidence),
            ],
            cwd=origin,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )
        assert direct.returncode != 0 and "No module named 'tools'" not in direct.stderr
    elif mutation == "config":
        fixture.write_text("trailing whitespace \n", encoding="utf-8")
        git("add", "fixture")
        git("-c", "user.name=Gate", "-c", "user.email=gate@example.invalid", "commit", "-qm", "whitespace")
        whitespace_candidate = git("rev-parse", "HEAD").stdout.strip()
        monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
        monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.whitespace")
        monkeypatch.setenv("GIT_CONFIG_VALUE_0", "-trailing-space")
        attributes = tmp_path / "xdg" / "git" / "attributes"
        attributes.parent.mkdir(parents=True)
        attributes.write_text("* -whitespace\n", encoding="utf-8")
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
        command = quality_gate._tier_static_commands(
            origin,
            python=sys.executable,
            tier="change",
            base_sha=candidate,
            candidate_sha=whitespace_candidate,
            environment=quality_gate._git_environment(),
        )[0]
        assert subprocess.run(command.argv, env=command.environment, check=False).returncode != 0


def test_comparison_epoch_never_replaces_the_normal_candidate_wheel(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    semantic = SimpleNamespace(
        nodeid="tests/test_probe.py::test_boundary",
        invariant_id="security.authentication-untrusted-input",
        tier="change",
        execution_kind="unit",
        max_runtime_s=30,
        scratch_mb=64,
    )
    assert quality_gate._partition_evidence((semantic,))[0]["invariant_id"] == semantic.invariant_id

    source = tmp_path / "source"
    source.mkdir()
    accepted = tmp_path / "accepted"
    rejected = tmp_path / "rejected"
    accepted.mkdir()
    rejected.mkdir()
    candidate = "a" * 40
    baseline = "b" * 40
    baseline_bytes = b"baseline-wheel"
    expected = hashlib.sha256(baseline_bytes).hexdigest()
    observed_epochs: list[str] = []

    def git_output(_root: Path, *arguments: str) -> str:
        return "200" if arguments[-1] == candidate else "100"

    def runner(command: quality_gate.GateCommand) -> int:
        if command.name.endswith("wheel build"):
            assert command.environment is not None
            epoch = command.environment["SOURCE_DATE_EPOCH"]
            observed_epochs.append(epoch)
            dist = Path(command.argv[-1])
            built_wheel = dist / "friday.whl"
            built_wheel.write_bytes(b"candidate-wheel" if epoch == "200" else baseline_bytes)
            built_wheel.chmod(0o664)
        return 0

    monkeypatch.setattr(quality_gate, "_git_output", git_output)
    monkeypatch.setattr(
        quality_gate,
        "_candidate_projection",
        lambda *_args, **_kwargs: quality_gate.nullcontext(source),
    )
    ordinary = tmp_path / "ordinary"
    ordinary.mkdir()
    _, _, ordinary_comparison, ordinary_site, ordinary_python = quality_gate._build_reusable_wheel(
        source,
        ordinary,
        candidate_sha=candidate,
        python="python",
        environment={},
        runner=runner,
    )
    assert ordinary_comparison is None and ordinary_site.name == "site-packages"
    assert ordinary_python == "python"
    assert quality_gate._tier_result_identity("exact-release", False)[2] is True
    assert quality_gate._tier_result_identity("change", False)[2] is False
    assert quality_gate._tier_result_identity("nightly", False) == (
        "friday.quality-gate-observation.v1",
        "observed",
        False,
    )
    assert quality_gate._tier_result_identity("exact-release", True) == (
        "friday.quality-gate-measurement.v1",
        "measured",
        False,
    )
    wheel, candidate_digest, comparison_digest, _site, runtime_python = quality_gate._build_reusable_wheel(
        source,
        accepted,
        candidate_sha=candidate,
        python="python",
        environment={},
        runner=runner,
        comparison_epoch_sha=baseline,
        comparison_sha256=expected,
    )
    assert hashlib.sha256(wheel.read_bytes()).hexdigest() == candidate_digest != expected
    assert stat.S_IMODE(wheel.stat().st_mode) == 0o600
    assert comparison_digest == expected
    assert observed_epochs == ["200", "200", "100"]
    assert _site.name == "site-packages"
    assert runtime_python == "python"

    with pytest.raises(RuntimeError, match="differs from comparison bytes"):
        quality_gate._build_reusable_wheel(
            source,
            rejected,
            candidate_sha=candidate,
            python="python",
            environment={},
            runner=runner,
            comparison_epoch_sha=baseline,
            comparison_sha256="0" * 64,
        )

    oversized = tmp_path / "oversized.whl"
    with oversized.open("wb") as stream:
        stream.truncate(64 * 1024 * 1024 + 1)
    with pytest.raises(RuntimeError, match="unsafe metadata"):
        quality_gate._bounded_wheel_sha256(oversized)

    evidence = tmp_path / "evidence"
    evidence.mkdir(mode=0o700)
    evidence_fd = quality_gate._open_evidence_directory(evidence)
    moved = tmp_path / "moved-evidence"
    evidence.rename(moved)
    evidence.mkdir(mode=0o700)
    with pytest.raises(RuntimeError, match="identity or contents changed"):
        quality_gate._require_evidence_directory(evidence, evidence_fd, ())
    os.close(evidence_fd)

    injected = tmp_path / "injected-evidence"
    injected.mkdir(mode=0o700)
    injected_fd = quality_gate._open_evidence_directory(injected)
    (injected / "foreign").touch()
    with pytest.raises(RuntimeError, match="identity or contents changed"):
        quality_gate._require_evidence_directory(injected, injected_fd, ())
    os.close(injected_fd)
