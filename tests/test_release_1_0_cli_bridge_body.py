"""Real CLI bridge ownership/SQLite/client lifecycle with local HTTP transports.

Only HTTP transport and the three endless loop boundaries are scripted. Parser,
configuration, bridge construction/run, private queue and kernel lease are real.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import sys
from contextlib import contextmanager

import httpx
import pytest

from friday import cli
from friday.diagnostics.runtime_lease import ProcessLease, inspect_process_lease
from friday.telegram_bridge import TelegramBridge
from friday.telegram_bridge import _transport as transport

PROTOCOL = "friday.telegram-bridge.v1"
TOKEN = "12345:synthetic-bridge-lifecycle-token"
PRIVATE_ERROR = "owned-private-startup-detail"


@contextmanager
def _observed(monkeypatch, settings, tmp_path):
    queue = tmp_path / "owned-inbox.sqlite3"
    lock = queue.with_name(queue.name + ".lock")
    monkeypatch.setattr(sys, "argv", ["friday", "telegram-bridge"])
    monkeypatch.setattr(cli, "configure_logging", lambda *_a, **_k: None)
    monkeypatch.setenv("FRIDAY_TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("FRIDAY_TELEGRAM_INBOX_DB_PATH", str(queue))
    monkeypatch.setenv("FRIDAY_BACKEND_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("FRIDAY_TELEGRAM_PROXY", "")
    monkeypatch.setenv("FRIDAY_BACKEND_CA_FILE", "")
    monkeypatch.setenv("FRIDAY_OBSIDIAN_ENABLED", "0")
    monkeypatch.setenv("FRIDAY_MCP_ENABLED", "0")
    bridges, inboxes, clients, requests, tasks = [], [], [], [], []
    real_bridge_init = TelegramBridge.__init__
    real_inbox_init = transport._UpdateInbox.__init__
    handlers = [(h, h.formatter) for h in logging.getLogger().handlers]
    levels = {name: logging.getLogger(name).level for name in ("httpx", "httpcore")}

    def bridge_init(self, config):
        real_bridge_init(self, config)
        bridges.append(self)
        assert self._inbox._instance is None

    def inbox_init(self, path):
        assert path == str(queue)
        lease = inspect_process_lease(lock, protocol=PROTOCOL)
        assert lease["active"] is True and lease["pid"] == os.getpid()
        real_inbox_init(self, path)
        inboxes.append(self)

    monkeypatch.setattr(TelegramBridge, "__init__", bridge_init)
    monkeypatch.setattr(transport._UpdateInbox, "__init__", inbox_init)
    observation = {
        "queue": queue,
        "lock": lock,
        "bridges": bridges,
        "inboxes": inboxes,
        "clients": clients,
        "requests": requests,
        "tasks": tasks,
    }
    try:
        yield observation
    finally:
        # Root-owned fallback happens AFTER every oracle. It must never turn a
        # leaked resource into passing cleanup evidence for the product.
        for client in clients:
            if not client.is_closed:
                asyncio.run(client.aclose())
        for bridge in bridges:
            bridge._inbox.close()
            bridge._lease.release()
        for handler, formatter in handlers:
            handler.setFormatter(formatter)
        for name, level in levels.items():
            logging.getLogger(name).setLevel(level)
        assert not (settings.state_dir / "backend.lock").exists()


def _clients(monkeypatch, seen, *, fail_second=False):
    real_client = httpx.AsyncClient
    construction = []

    def reply(request):
        seen["requests"].append(request)
        assert request.method == "POST"
        assert request.url.host == "api.telegram.org"
        if request.url.path == f"/bot{TOKEN}/getMe":
            assert json.loads(request.content) == {}
            return httpx.Response(200, json={"ok": True, "result": {"username": "owned_test_bot"}})
        assert request.url.path == f"/bot{TOKEN}/setMyCommands"
        commands = json.loads(request.content)["commands"]
        assert {"chat", "help", "inbox"} <= {c["command"] for c in commands}
        assert not {"obsidian", "obsidian_alias"} & {c["command"] for c in commands}
        return httpx.Response(200, json={"ok": True, "result": True})

    class LocalClient(real_client):
        def __init__(self, **kwargs):
            assert inspect_process_lease(seen["lock"], protocol=PROTOCOL)["active"] is True
            assert len(seen["inboxes"]) == 1
            assert kwargs["trust_env"] is False
            construction.append(dict(kwargs))
            if len(construction) == 2 and fail_second:
                raise RuntimeError(PRIVATE_ERROR)
            super().__init__(**kwargs, transport=httpx.MockTransport(reply))
            seen["clients"].append(self)

    monkeypatch.setattr(httpx, "AsyncClient", LocalClient)
    return construction


def _released(seen):
    assert len(seen["bridges"]) == len(seen["inboxes"]) == 1
    bridge = seen["bridges"][0]
    assert bridge._inbox._instance is None, "bridge left its SQLite inbox open"
    assert bridge._lease.acquired is False, "bridge retained its runtime lease"
    assert inspect_process_lease(seen["lock"], protocol=PROTOCOL)["active"] is False
    for inbox in seen["inboxes"]:
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            inbox._conn.execute("SELECT 1")
    assert all(client.is_closed for client in seen["clients"])
    assert all(task.done() for task in seen["tasks"])
    with ProcessLease(seen["lock"], protocol=PROTOCOL):
        assert inspect_process_lease(seen["lock"], protocol=PROTOCOL)["pid"] == os.getpid()


@pytest.mark.parametrize("mode", ["normal", "loop_error", "client_error"])
def test_cli_bridge_real_run_releases_queue_clients_lease_and_tasks(
    settings, tmp_path, monkeypatch, caplog, mode
):
    with _observed(monkeypatch, settings, tmp_path) as seen:
        construction = _clients(monkeypatch, seen, fail_second=mode == "client_error")
        entered, finished = [], []
        ready = asyncio.Event()

        def loop(name):
            async def bounded(self, *_clients):
                seen["tasks"].append(asyncio.current_task())
                entered.append(name)
                if len(entered) == 3:
                    ready.set()
                try:
                    await asyncio.wait_for(ready.wait(), timeout=2)
                    if mode == "normal":
                        self._running = False
                        return
                    if name == "poll":
                        raise RuntimeError(PRIVATE_ERROR)
                    await asyncio.Future()
                finally:
                    finished.append(name)

            return bounded

        for method, name in [
            ("_poll_loop", "poll"),
            ("_outbound_loop", "outbound"),
            ("_poll_watchdog", "watchdog"),
        ]:
            monkeypatch.setattr(TelegramBridge, method, loop(name))
        with caplog.at_level(logging.ERROR), pytest.raises(SystemExit) as exited:
            cli.main()
        assert exited.value.code == (0 if mode == "normal" else 2)
        assert len(construction) == 2
        assert "verify" not in construction[0] and construction[0]["proxy"] is None
        assert construction[1]["verify"].check_hostname is True
        assert construction[1]["verify"].verify_mode != 0
        assert construction[1]["timeout"].read == settings.bridge_backend_timeout_sec
        bridge = seen["bridges"][0]
        assert bridge.config.inbox_db_path == str(seen["queue"])
        assert bridge.config.allowed_chat_ids == settings.telegram_effective_allowed_chat_ids
        assert bridge.config.backend_url == "http://127.0.0.1:9"
        assert bridge.config.bridge_secret == settings.telegram_bridge_secret
        assert seen["queue"].stat().st_mode & 0o777 == 0o600
        if mode == "client_error":
            assert entered == finished == seen["requests"] == []
            assert len(seen["clients"]) == 1
        else:
            assert sorted(entered) == sorted(finished) == ["outbound", "poll", "watchdog"]
            assert len(seen["clients"]) == len(seen["requests"]) == 2
            assert bridge._bot_username == "owned_test_bot"
        _released(seen)
        assert PRIVATE_ERROR not in caplog.text and TOKEN not in caplog.text
        if mode != "normal":
            assert [r.getMessage() for r in caplog.records if r.name == "friday.cli"] == [
                "CLI command failed (RuntimeError)"
            ]


def test_cli_bridge_held_lease_refuses_before_queue_or_http(settings, tmp_path, monkeypatch, caplog):
    with _observed(monkeypatch, settings, tmp_path) as seen:
        construction = _clients(monkeypatch, seen)
        with ProcessLease(seen["lock"], protocol=PROTOCOL):
            before = seen["lock"].read_bytes()
            with caplog.at_level(logging.ERROR), pytest.raises(SystemExit) as exited:
                cli.main()
            assert exited.value.code == 2
            assert construction == seen["inboxes"] == seen["clients"] == []
            assert len(seen["bridges"]) == 1
            assert seen["bridges"][0]._lease.acquired is False
            assert not seen["queue"].exists()
            assert seen["lock"].read_bytes() == before
            assert inspect_process_lease(seen["lock"], protocol=PROTOCOL)["active"] is True
            assert [r.getMessage() for r in caplog.records if r.name == "friday.cli"] == [
                "CLI command failed (RuntimeLeaseError)"
            ]
        assert inspect_process_lease(seen["lock"], protocol=PROTOCOL)["active"] is False


@pytest.mark.parametrize("bad", ["token", "allowlist", "backend_url"])
def test_cli_bridge_invalid_config_refuses_without_queue_or_client(settings, tmp_path, monkeypatch, bad):
    with _observed(monkeypatch, settings, tmp_path) as seen:
        construction = _clients(monkeypatch, seen)
        if bad == "token":
            monkeypatch.setenv("FRIDAY_TELEGRAM_BOT_TOKEN", "")
        elif bad == "allowlist":
            monkeypatch.setenv("FRIDAY_TELEGRAM_ALLOWED_CHAT_IDS", "")
            monkeypatch.setenv("FRIDAY_TELEGRAM_OWNER_CHAT_IDS", "")
        else:
            monkeypatch.setenv("FRIDAY_BACKEND_URL", "https://user:private@127.0.0.1:9")
        with pytest.raises(SystemExit) as exited:
            cli.main()
        assert exited.value.code == 2
        assert construction == seen["bridges"] == seen["inboxes"] == []
        assert not seen["queue"].exists() and not seen["lock"].exists()


def test_cli_bridge_real_invalid_ca_releases_inbox_and_kernel_lease(settings, tmp_path, monkeypatch):
    with _observed(monkeypatch, settings, tmp_path) as seen:
        ca = tmp_path / "invalid-ca.pem"
        ca.write_text("this is not a certificate\n")
        monkeypatch.setenv("FRIDAY_BACKEND_URL", "https://127.0.0.1:9")
        monkeypatch.setenv("FRIDAY_BACKEND_CA_FILE", str(ca))
        construction = _clients(monkeypatch, seen)
        # Focus on resource release regardless of how the CLI surfaces the SSL
        # error. The refusal is actual OpenSSL validation of an owned bad file.
        observed = None
        try:
            cli.main()
        except (SystemExit, OSError) as error:
            observed = error
        assert observed is not None
        assert construction == seen["clients"] == []
        assert ca.read_text() == "this is not a certificate\n"
        _released(seen)


def test_cli_bridge_ssl_context_failure_retires_open_queue_and_running_state(
    settings, tmp_path, monkeypatch, caplog
):
    with _observed(monkeypatch, settings, tmp_path) as seen:
        construction = _clients(monkeypatch, seen)

        def failed_ssl_context(**_kwargs):
            assert len(seen["inboxes"]) == 1
            assert inspect_process_lease(seen["lock"], protocol=PROTOCOL)["active"]
            raise OSError(PRIVATE_ERROR)

        monkeypatch.setattr(httpx, "create_ssl_context", failed_ssl_context)
        # Match the existing real-invalid-CA oracle: OSError propagates from
        # this CLI path. Resource ownership and absence of logged secrets are
        # independent of an unsupported SystemExit conversion.
        with caplog.at_level(logging.ERROR), pytest.raises(OSError):
            cli.main()
        assert construction == seen["clients"] == []
        assert seen["bridges"][0]._running is False
        _released(seen)
        assert PRIVATE_ERROR not in caplog.text and TOKEN not in caplog.text
