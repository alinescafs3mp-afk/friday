"""Exact CLI to TUI body/lifecycle oracles for the release-1.0 surface.

The in-process cases execute cli.main through the real parser, service-role
preflight, _tui and tui.run. They replace only curses' terminal wrapper and
execvp boundary. Filesystem state and the private atomic save are real.
The PTY case separately exercises the real curses wrapper in an owned child.
"""

from __future__ import annotations

import logging
import os
import pty
import signal
import stat
import subprocess
import sys
import termios
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from friday import cli, config, tui


class _ExecObserved(Exception):
    pass


class _ScriptedWindow:
    def __init__(self, keys: list[int]) -> None:
        self.keys = list(keys)
        self.writes: list[str] = []
        self.keypad_enabled = False

    def getmaxyx(self) -> tuple[int, int]:
        return 24, 100

    def erase(self) -> None:
        return None

    def border(self) -> None:
        return None

    def addnstr(self, _y: int, _x: int, text: str, _limit: int, _attr: int = 0) -> None:
        self.writes.append(text)

    def noutrefresh(self) -> None:
        return None

    def keypad(self, enabled: bool) -> None:
        self.keypad_enabled = enabled

    def getch(self) -> int:
        if not self.keys:
            raise AssertionError("scripted TUI input exhausted")
        return self.keys.pop(0)


def _install_boundary(
    monkeypatch: pytest.MonkeyPatch,
    settings: Any,
    target: Path,
    keys: list[int],
    execvp: Callable[[str, list[str]], Any],
) -> tuple[_ScriptedWindow, list[str]]:
    import curses

    window = _ScriptedWindow(keys)
    wrapper_events: list[str] = []

    def wrapper(function: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        assert kwargs == {}
        wrapper_events.append("enter")
        try:
            return function(window, *args)
        finally:
            wrapper_events.append("exit")

    # Keep main's configuration reads inside the already isolated settings
    # fixture. The explicit file remains the real TUI read/write target.
    monkeypatch.setattr(config, "load_local_env_file", lambda *_a, **_k: [])
    monkeypatch.setattr(config, "load_settings", lambda *_a, **_k: settings)
    monkeypatch.setattr(cli, "configure_logging", lambda *_a, **_k: None)
    monkeypatch.setattr(curses, "wrapper", wrapper)
    monkeypatch.setattr(curses, "curs_set", lambda *_a, **_k: None)
    monkeypatch.setattr(curses, "doupdate", lambda: None)
    monkeypatch.setattr(tui.os, "execvp", execvp)
    monkeypatch.setenv("FRIDAY_ENV_FILE", str(target))
    monkeypatch.setattr(
        sys,
        "argv",
        ["friday", "--env-file", str(target), "tui"],
    )
    return window, wrapper_events


def _dirty_start_keys(value: str) -> list[int]:
    # Main menu -> config -> edit first field -> commit -> main -> Start.
    return [
        tui.KEY_ENTER,
        tui.KEY_ENTER,
        *(ord(character) for character in value),
        tui.KEY_ENTER,
        ord("q"),
        ord("j"),
        tui.KEY_ENTER,
    ]


def test_cli_tui_quit_runs_real_body_and_returns_through_wrapper(
    settings: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / ".env.local"
    original = "# no TUI mutation\n"
    target.write_text(original, encoding="utf-8")
    target.chmod(0o600)

    def forbidden_exec(_file: str, _argv: list[str]) -> None:
        pytest.fail("quit path reached execvp")

    window, wrapper_events = _install_boundary(
        monkeypatch,
        settings,
        target,
        [ord("q")],
        forbidden_exec,
    )
    with pytest.raises(SystemExit) as exited:
        cli.main()

    assert exited.value.code == 0
    assert wrapper_events == ["enter", "exit"]
    assert window.keypad_enabled is True
    assert target.read_text(encoding="utf-8") == original
    assert not (settings.state_dir / "backend.lock").exists()


def test_cli_tui_dirty_start_saves_private_env_before_exact_up_exec(
    settings: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / ".env.local"
    target.write_text("# retained\nOTHER=keep\n", encoding="utf-8")
    target.chmod(0o644)
    value = "http://127.0.0.1:9/v1"
    calls: list[dict[str, Any]] = []
    wrapper_events: list[str] = []

    def capture_exec(file: str, argv: list[str]) -> None:
        calls.append(
            {
                "file": file,
                "argv": list(argv),
                "text": target.read_text(encoding="utf-8"),
                "mode": stat.S_IMODE(target.stat().st_mode),
                "wrapper_events": list(wrapper_events),
            }
        )
        raise _ExecObserved

    _window, installed_events = _install_boundary(
        monkeypatch,
        settings,
        target,
        _dirty_start_keys(value),
        capture_exec,
    )
    wrapper_events = installed_events
    with pytest.raises(_ExecObserved):
        cli.main()

    assert calls == [
        {
            "file": sys.executable,
            "argv": [
                sys.executable,
                "-m",
                "friday.cli",
                "--env-file",
                str(target),
                "up",
            ],
            "text": f"# retained\nOTHER=keep\nFRIDAY_LLM_BASE_URL={value}\n",
            "mode": 0o600,
            "wrapper_events": ["enter", "exit"],
        }
    ]
    assert not (settings.state_dir / "backend.lock").exists()


def test_cli_tui_failed_autosave_never_execs_up(
    settings: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # An existing directory is a deterministic filesystem refusal even as root:
    # atomic os.replace(temp_file, directory) raises IsADirectoryError, and _save
    # catches it as OSError. A correct launcher remains in the loop and consumes
    # the final q instead of starting with stale values.
    target = tmp_path / "env-is-a-directory"
    target.mkdir()
    exec_calls: list[tuple[str, list[str]]] = []

    def forbidden_exec(file: str, argv: list[str]) -> None:
        exec_calls.append((file, list(argv)))
        pytest.fail("dirty autosave failed but TUI still executed up")

    keys = [*_dirty_start_keys("http://127.0.0.1:9/v1"), ord("q")]
    window, wrapper_events = _install_boundary(
        monkeypatch,
        settings,
        target,
        keys,
        forbidden_exec,
    )
    with pytest.raises(SystemExit) as exited:
        cli.main()

    assert exited.value.code == 0
    assert exec_calls == []
    assert wrapper_events == ["enter", "exit"]
    assert any("Ошибка записи" in text for text in window.writes)
    assert target.is_dir() and list(target.iterdir()) == []
    assert not list(tmp_path.glob(".env-is-a-directory.*.tmp"))
    assert not (settings.state_dir / "backend.lock").exists()


def test_cli_tui_exec_failure_returns_redacted_exit_and_clean_wrapper(
    settings: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    target = tmp_path / ".env.local"
    original = "# exact exec failure\n"
    target.write_text(original, encoding="utf-8")
    target.chmod(0o600)
    private_detail = "private-exec-detail-must-not-leak"
    calls: list[tuple[str, list[str]]] = []
    wrapper_events: list[str] = []

    def failing_exec(file: str, argv: list[str]) -> None:
        calls.append((file, list(argv)))
        assert wrapper_events == ["enter", "exit"]
        raise FileNotFoundError(private_detail)

    _window, installed_events = _install_boundary(
        monkeypatch,
        settings,
        target,
        [ord("j"), tui.KEY_ENTER],
        failing_exec,
    )
    wrapper_events = installed_events
    with caplog.at_level(logging.ERROR, logger="friday.cli"), pytest.raises(SystemExit) as exited:
        cli.main()

    assert exited.value.code == 2
    assert calls == [
        (
            sys.executable,
            [
                sys.executable,
                "-m",
                "friday.cli",
                "--env-file",
                str(target),
                "up",
            ],
        )
    ]
    records = [record.getMessage() for record in caplog.records if record.name == "friday.cli"]
    assert records == ["CLI command failed (FileNotFoundError)"]
    assert private_detail not in caplog.text
    assert target.read_text(encoding="utf-8") == original
    assert not (settings.state_dir / "backend.lock").exists()


def _closed_child_environment(settings: Any, target: Path, source_root: Path) -> dict[str, str]:
    environment = dict(os.environ)
    for name in tuple(environment):
        upper = name.upper()
        if upper.startswith(("FRIDAY_", "JERICHO_", "OPENAI_", "ANTHROPIC_")) or upper in {
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "ALL_PROXY",
            "NO_PROXY",
        }:
            environment.pop(name, None)
    environment.update(
        {
            "HOME": str(settings.home),
            "XDG_CONFIG_HOME": str(settings.home / "xdg-config"),
            "XDG_CACHE_HOME": str(settings.home / "xdg-cache"),
            "TERM": "xterm",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "PYTHONUTF8": "1",
            "PYTHONPATH": str(source_root),
            "FRIDAY_HOME": str(settings.home),
            "FRIDAY_ENV_FILE": str(target),
            "FRIDAY_PROFILE": "qwen36-27b-nvfp4-nvidia",
            "FRIDAY_API_TOKEN": "A" * 48,
            "FRIDAY_TELEGRAM_BRIDGE_SECRET": "B" * 48,
            "FRIDAY_API_HOST": "127.0.0.1",
            "FRIDAY_API_PORT": "18777",
            "FRIDAY_API_REQUIRE_TOKEN_ON_LOOPBACK": "1",
            "FRIDAY_CORS_ORIGINS": "http://127.0.0.1:18777",
            "FRIDAY_LLM_ENABLED": "0",
            "FRIDAY_EMBEDDINGS_ENABLED": "0",
            "FRIDAY_WORKERS_ENABLED": "0",
            "FRIDAY_CODE_EXECUTION_ENABLED": "0",
            "FRIDAY_MCP_ENABLED": "0",
            "FRIDAY_OBSIDIAN_ENABLED": "0",
            "FRIDAY_SEMANTIC_SUPERVISOR_MODE": "off",
        }
    )
    return environment


def test_cli_tui_real_owned_pty_quit_restores_termios(
    settings: Any,
    tmp_path: Path,
) -> None:
    target = tmp_path / "pty.env"
    original = "# owned PTY; no service start\n"
    target.write_text(original, encoding="utf-8")
    target.chmod(0o600)
    # Resolve from the imported pinned product so the owned draft is runnable
    # both before and after the root copies it under tests/.
    source_root = Path(cli.__file__).resolve().parents[1]
    environment = _closed_child_environment(settings, target, source_root)

    master_fd, slave_fd = pty.openpty()
    before = termios.tcgetattr(slave_fd)
    process: subprocess.Popen[bytes] | None = None
    timed_out = False
    returncode: int | None = None
    after: list[Any] | None = None
    try:
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "friday.cli",
                "--env-file",
                str(target),
                "tui",
            ],
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            cwd=source_root,
            env=environment,
            close_fds=True,
            start_new_session=True,
        )
        # Canonical mode may retain an early q until newline; curses then consumes
        # q as soon as its real wrapper reaches getch. No readiness sleep/race.
        os.write(master_fd, b"q\n")
        try:
            returncode = process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            timed_out = True
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=2)
        after = termios.tcgetattr(slave_fd)
    finally:
        if process is not None and process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=2)
        os.close(master_fd)
        os.close(slave_fd)

    assert timed_out is False
    assert returncode == 0
    assert after == before
    assert target.read_text(encoding="utf-8") == original
    assert not (settings.state_dir / "backend.lock").exists()
