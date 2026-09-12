"""Real isolated server/up processes, loopback readiness and singleton shutdown."""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
from contextlib import contextmanager, suppress
from pathlib import Path
from urllib.error import URLError
from urllib.request import ProxyHandler, Request, build_opener

import pytest

from friday.diagnostics.runtime_lease import ProcessLease, inspect_process_lease

SOURCE = Path(__file__).resolve().parents[1]


def _ports():
    with socket.socket() as first, socket.socket() as second:
        first.bind(("127.0.0.1", 0))
        second.bind(("127.0.0.1", 0))
        return first.getsockname()[1], second.getsockname()[1]


def _request(port, path, token):
    request = Request(f"http://127.0.0.1:{port}{path}", headers={"Authorization": f"Bearer {token}"})
    with build_opener(ProxyHandler({})).open(request, timeout=0.5) as response:
        return response.status, json.load(response)


def _listens(port):
    with socket.socket() as sock:
        sock.settimeout(0.2)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def _parent(pid):
    # Linux runtime contract; parse after comm, which may contain spaces or ')'.
    text = (Path("/proc") / str(pid) / "stat").read_text()
    return int(text.rsplit(")", 1)[1].split()[1])


def _group_absent(pid):
    try:
        os.killpg(pid, 0)
    except ProcessLookupError:
        return True
    return False


def _env(settings, port):
    # Parent canonical runner already provides a sealed environment. Child flags
    # are explicit, and PYTHONPATH pins up's child after it changes to temp HOME.
    env = dict(os.environ)
    env.update(
        PYTHONPATH=str(SOURCE),
        PYTHONDONTWRITEBYTECODE="1",
        PYTHONUNBUFFERED="1",
        FRIDAY_HOME=str(settings.home),
        FRIDAY_API_HOST="127.0.0.1",
        FRIDAY_API_PORT=str(port),
        FRIDAY_API_TOKEN=settings.api_token,
        FRIDAY_API_REQUIRE_TOKEN_ON_LOOPBACK="1",
        FRIDAY_LLM_ENABLED="0",
        FRIDAY_EMBEDDINGS_ENABLED="0",
        FRIDAY_WORKERS_ENABLED="0",
        FRIDAY_CODE_EXECUTION_ENABLED="0",
        FRIDAY_SECONDARY_LLM_ENABLED="0",
        FRIDAY_SECONDARY_LLM_MODE="disabled",
        FRIDAY_SEMANTIC_SUPERVISOR_MODE="off",
        FRIDAY_MCP_ENABLED="0",
        FRIDAY_TELEGRAM_BOT_TOKEN="",
        FRIDAY_OBSIDIAN_ENABLED="0",
        FRIDAY_MEMORY_VAULT_MODE="disabled",
        GIT_OPTIONAL_LOCKS="0",
    )
    for name in (
        "FRIDAY_ENV_FILE",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
    ):
        env.pop(name, None)
    return env


@contextmanager
def _child(settings, tmp_path, entry, port, label):
    log = tmp_path / f"{label}.log"
    with log.open("xb") as stream:
        log.chmod(0o600)
        args = [sys.executable, "-B", "-m", "friday.cli", entry]
        if entry == "up":
            args.append("--no-bridge")
        process = subprocess.Popen(
            args,
            cwd=SOURCE,
            env=_env(settings, port),
            stdout=stream,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        owned_backend = None
        try:
            yield process, log
        finally:
            # Capture only the child proven to belong to this exact supervisor
            # before terminating its parent. No global process-name cleanup.
            lease = inspect_process_lease(settings.state_dir / "backend.lock", protocol="friday.backend.v1")
            candidate = lease.get("pid")
            if entry == "up" and type(candidate) is int and candidate != process.pid:
                with suppress(FileNotFoundError, ProcessLookupError):
                    if _parent(candidate) == process.pid and os.getpgid(candidate) == candidate:
                        owned_backend = candidate
            if process.poll() is None:
                process.send_signal(signal.SIGTERM)
                try:
                    process.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    if owned_backend is not None:
                        with suppress(ProcessLookupError):
                            os.killpg(owned_backend, signal.SIGKILL)
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait(timeout=5)
            if owned_backend is not None and not _group_absent(owned_backend):
                with suppress(ProcessLookupError):
                    os.killpg(owned_backend, signal.SIGKILL)
            assert process.poll() is not None and _group_absent(process.pid), (
                "owned CLI group survived cleanup"
            )


def _ready(process, port, token, log):
    deadline = time.monotonic() + 40
    while time.monotonic() < deadline:
        assert process.poll() is None, log.read_text(errors="replace")[-4000:]
        try:
            status, body = _request(port, "/api/health", token)
            if status == 200:
                return body
        except (URLError, TimeoutError, ConnectionError):
            pass
        time.sleep(0.05)
    raise AssertionError("owned server never became ready: " + log.read_text(errors="replace")[-4000:])


@pytest.mark.parametrize("entry", ["server", "up"])
def test_cli_real_server_ready_singleton_and_graceful_shutdown(settings, tmp_path, entry):
    assert not settings.llm_enabled and not settings.workers_enabled
    first_port, second_port = _ports()
    assert first_port != second_port
    lock = settings.state_dir / "backend.lock"
    with _child(settings, tmp_path, entry, first_port, "first") as (first, log):
        _ready(first, first_port, settings.api_token, log)
        status, me = _request(first_port, "/api/me", settings.api_token)
        assert status == 200 and me["actor"]["preset_key"] == "owner"
        lease = inspect_process_lease(lock, protocol="friday.backend.v1")
        assert lease["active"] is True
        backend_pid = lease["pid"]
        assert type(backend_pid) is int
        if entry == "server":
            assert backend_pid == first.pid
        else:
            assert _parent(backend_pid) == first.pid and os.getpgid(backend_pid) == backend_pid
            backend_log = settings.log_dir / "backend.log"
            assert backend_log.is_file() and backend_log.stat().st_mode & 0o777 == 0o600
            assert not (settings.log_dir / "telegram-bridge.log").exists()
        # A different free port proves that refusal is about shared runtime
        # ownership, not an accidental collision at the listening socket.
        with _child(settings, tmp_path, entry, second_port, "duplicate") as (duplicate, duplicate_log):
            code = duplicate.wait(timeout=20)
            assert code != 0, duplicate_log.read_text(errors="replace")[-4000:]
            if entry == "up":
                assert code == 2
            assert not _listens(second_port)
            still = inspect_process_lease(lock, protocol="friday.backend.v1")
            assert still["active"] is True and still["pid"] == backend_pid
            assert _request(first_port, "/api/me", settings.api_token)[0] == 200
        first.send_signal(signal.SIGTERM)
        code = first.wait(timeout=20)
        assert code in (0, -signal.SIGTERM), log.read_text(errors="replace")[-4000:]
        assert _group_absent(first.pid)
        if entry == "up":
            assert _group_absent(backend_pid)
        assert not _listens(first_port)
        assert inspect_process_lease(lock, protocol="friday.backend.v1")["active"] is False
        with ProcessLease(lock, protocol="friday.backend.v1"):
            assert inspect_process_lease(lock, protocol="friday.backend.v1")["pid"] == os.getpid()
        assert settings.api_token not in log.read_text(errors="replace")
