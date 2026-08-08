"""Runtime proofs for the synthetic battery's immutable worker boundary."""

from __future__ import annotations

import contextlib
import json
import os
import socket
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import synthetic_live_battery as battery  # noqa: E402


class _TcpEcho:
    def __init__(self) -> None:
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(16)
        self.listener.settimeout(0.1)
        self.port = int(self.listener.getsockname()[1])
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def __enter__(self) -> _TcpEcho:
        self._thread.start()
        return self

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                connection, _address = self.listener.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            with connection, contextlib.suppress(OSError):
                payload = connection.recv(1024)
                connection.sendall(payload[::-1])

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        del exc_type, exc, traceback
        self._stop.set()
        self.listener.close()
        self._thread.join(timeout=2)


class _UnixEcho:
    def __init__(self, path: Path) -> None:
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.bind(str(path))
        os.chmod(path, 0o600)
        self.listener.listen(16)
        self.listener.settimeout(0.1)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                connection, _address = self.listener.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            with connection, contextlib.suppress(OSError):
                payload = connection.recv(1024)
                connection.sendall(b"uds:" + payload)

    def close(self) -> None:
        self._stop.set()
        self.listener.close()
        self._thread.join(timeout=2)


def _endpoint_settings(port: int) -> SimpleNamespace:
    endpoint = f"http://127.0.0.1:{port}/v1"
    return SimpleNamespace(
        llm_base_url=endpoint,
        embeddings_base_url=endpoint,
        rerank_base_url=endpoint,
    )


def _pass_context(tmp_path: Path) -> battery.PassContext:
    home = tmp_path / "pass-01" / "home"
    home.mkdir(parents=True, mode=0o700)
    home.chmod(0o700)
    battery._prepare_process_scratch(home)
    evidence = tmp_path / "pass-01" / "evidence" / "raw.jsonl"
    evidence.parent.mkdir(mode=0o700)
    evidence.parent.chmod(0o700)
    return battery.PassContext(
        battery_id="A",
        pass_id="A-P01",
        pass_index=1,
        seed=2026080802,
        clock=battery.FIXED_CLOCK,
        timezone=battery.FIXED_TIMEZONE,
        manifest_sha256=battery.FROZEN_MANIFEST_SHA256["A"],
        home=home,
        evidence_path=evidence,
    )


def _manifest_and_cases() -> tuple[dict[str, Any], list[battery.ExpandedCase]]:
    manifest = battery.load_manifest(battery.MANIFEST_PATHS["A"])
    cases = [case for case in battery.expand_manifest_cases(manifest) if case.pass_index == 1]
    return manifest, cases


def test_candidate_snapshot_is_closed_immutable_and_ignores_source_bytecode(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    package = source / "friday"
    package.mkdir(parents=True, mode=0o700)
    package.chmod(0o700)
    module = package / "module.py"
    module.write_bytes(b"VALUE = 'sealed'\n")
    module.chmod(0o600)
    bytecode = package / "__pycache__" / "module.cpython-314.pyc"
    bytecode.parent.mkdir(mode=0o700)
    bytecode.write_bytes(b"untrusted ignored bytecode")

    snapshot = battery._CandidateSourceSnapshot(
        source_root=source,
        relative_paths=("friday/module.py",),
    )
    try:
        module.write_bytes(b"VALUE = 'changed after sealing'\n")
        assert (snapshot.root / "friday/module.py").read_bytes() == b"VALUE = 'sealed'\n"
        assert not (snapshot.root / "friday/__pycache__").exists()
        assert battery._snapshot_candidate_paths(snapshot.root) == ("friday/module.py",)
        assert (
            battery._candidate_source_digest(
                root=snapshot.root,
                relative_paths=snapshot.relative_paths,
            )
            == snapshot.sha256
        )
    finally:
        snapshot.close()


def test_candidate_digest_rejects_symlinked_files_and_ancestors(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir(mode=0o700)
    target = source / "target.py"
    target.write_text("SAFE = True\n", encoding="utf-8")
    linked_file = source / "linked.py"
    linked_file.symlink_to(target)
    real_package = source / "real-package"
    real_package.mkdir()
    (real_package / "module.py").write_text("SAFE = True\n", encoding="utf-8")
    (source / "linked-package").symlink_to(real_package, target_is_directory=True)

    with pytest.raises(
        battery.BatteryContractError,
        match="candidate_source_symlink_forbidden",
    ):
        battery._candidate_source_digest(root=source, relative_paths=("linked.py",))
    with pytest.raises(
        battery.BatteryContractError,
        match="candidate_source_symlink_forbidden",
    ):
        battery._candidate_source_digest(
            root=source,
            relative_paths=("linked-package/module.py",),
        )


def test_seccomp_blocks_process_clones_but_allows_python_threads() -> None:
    probe = f"""
import errno
import json
import os
import subprocess
import sys
import threading
sys.path.insert(0, {str(ROOT / "tools")!r})
import synthetic_live_battery as battery
battery._install_no_exec_seccomp()
values = []
thread = threading.Thread(target=lambda: values.append('thread-ok'))
thread.start()
thread.join(2)
if values != ['thread-ok'] or thread.is_alive():
    raise SystemExit(20)
try:
    pid = os.fork()
except OSError as exc:
    fork_blocked = exc.errno == errno.EPERM
else:
    if pid == 0:
        os._exit(0)
    os.waitpid(pid, 0)
    fork_blocked = False
try:
    subprocess.run([sys.executable, '-c', 'raise SystemExit(0)'], check=False)
except OSError as exc:
    exec_blocked = exc.errno == errno.EPERM
else:
    exec_blocked = False
print(json.dumps({{'thread': True, 'fork_blocked': fork_blocked, 'exec_blocked': exec_blocked}}))
"""
    completed = subprocess.run(  # noqa: S603 - closed local probe
        [sys.executable, "-s", "-P", "-B", "-c", probe],
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert completed.returncode == 0, battery._sha256_bytes(completed.stderr.encode())
    assert json.loads(completed.stdout) == {
        "thread": True,
        "fork_blocked": True,
        "exec_blocked": True,
    }


def test_host_unix_relay_has_one_fixed_tcp_upstream() -> None:
    with _TcpEcho() as upstream:
        endpoint = f"http://127.0.0.1:{upstream.port}/v1"
        with battery._HostEndpointRelays(
            {"model": endpoint, "embedding": endpoint, "reranker": endpoint}
        ) as relays:
            assert relays.directory is not None
            assert relays.directory.stat().st_mode & 0o777 == 0o700
            assert all(
                (relays.directory / name).stat().st_mode & 0o777 == 0o600
                for name in battery._RELAY_SOCKET_NAMES.values()
            )
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(2)
                client.connect(str(relays.directory / "model.sock"))
                client.sendall(b"synthetic-ping")
                assert client.recv(1024) == b"gnip-citehtnys"


def test_worker_bridge_routes_only_configured_tcp_tuple_to_fixed_unix_relay(
    tmp_path: Path,
) -> None:
    relay_root = tmp_path / "relays"
    relay_root.mkdir(mode=0o700)
    reserved = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    reserved.bind(("127.0.0.1", 0))
    port = int(reserved.getsockname()[1])
    settings = _endpoint_settings(port)
    servers = [_UnixEcho(relay_root / name) for name in battery._RELAY_SOCKET_NAMES.values()]
    for server in servers:
        server.start()
    try:
        with (
            battery._UnixRelayLoopbackBridge.from_settings(settings, relay_root) as bridge,
            battery.LocalEndpointNetworkGuard.from_settings(
                settings,
                relay_routes=bridge.routes,
            ) as guard,
            socket.socket(socket.AF_INET, socket.SOCK_STREAM) as client,
        ):
            client.settimeout(2)
            client.connect(("127.0.0.1", port))
            client.sendall(b"synthetic-ping")
            assert client.recv(1024) == b"uds:synthetic-ping"
        assert guard.allowed_attempts == 1
        assert guard.denied_attempts == 0
    finally:
        reserved.close()
        for server in servers:
            server.close()


def test_real_bwrap_sees_only_snapshot_scratch_and_fixed_relay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest, cases = _manifest_and_cases()
    context = _pass_context(tmp_path)
    real_run = battery._run_worker_bounded
    observed: dict[str, Any] = {}

    with _TcpEcho() as upstream:
        endpoint = f"http://127.0.0.1:{upstream.port}/v1"
        probe = f"""
import _socket
import json
import os
import pathlib
import socket
import sys
sys.path[:0] = ['/workspace', '/workspace/tools']
import synthetic_live_battery as battery
from friday.config import load_settings
from friday.server import create_app
request = json.loads(sys.stdin.buffer.read().decode('utf-8'))
settings = load_settings()
production_app = create_app(settings)
direct = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
direct.settimeout(1)
try:
    direct.connect(('127.0.0.1', {upstream.port}))
except OSError:
    direct_blocked = True
else:
    direct_blocked = False
finally:
    direct.close()
relay = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
relay.settimeout(2)
relay.connect('/run/friday-relays/model.sock')
relay.sendall(b'synthetic-ping')
relay_reply = relay.recv(1024).decode('ascii')
relay.close()
battery._install_no_exec_seccomp()
with (
    battery._UnixRelayLoopbackBridge.from_settings(settings) as bridge,
    battery.LocalEndpointNetworkGuard.from_settings(
        settings,
        relay_routes=bridge.routes,
    ) as guard,
    socket.socket(socket.AF_INET, socket.SOCK_STREAM) as guarded,
):
    guarded.settimeout(2)
    guarded.connect(('127.0.0.1', {upstream.port}))
    guarded.sendall(b'guarded-ping')
    guarded_reply = guarded.recv(1024).decode('ascii')
    guard_allowed = guard.allowed_attempts
candidate = pathlib.Path('/workspace/tools/synthetic_live_battery.py')
try:
    with candidate.open('ab') as handle:
        handle.write(b'x')
except OSError:
    snapshot_read_only = True
else:
    snapshot_read_only = False
result = {{
    'valid_request': battery._valid_worker_request(request),
    'root': str(battery.ROOT),
    'production_imports': production_app is not None,
    'direct_blocked': direct_blocked,
    'relay_reply': relay_reply,
    'guarded_reply': guarded_reply,
    'guard_allowed': guard_allowed,
    'snapshot_read_only': snapshot_read_only,
    'host_candidate_hidden': not pathlib.Path({str(ROOT / "OPEN.md")!r}).exists(),
    'sys_hidden': not pathlib.Path('/sys').exists(),
}}
print(json.dumps(result, sort_keys=True))
"""

        def run_probe(argv, **kwargs):  # noqa: ANN001, ANN202
            separator = argv.index("--")
            probe_argv = (
                *argv[: separator + 1],
                sys.executable,
                "-s",
                "-P",
                "-B",
                "-c",
                probe,
            )
            completed = real_run(probe_argv, **kwargs)
            assert completed.returncode == 0, battery._sha256_bytes(completed.stderr)
            assert not completed.timed_out
            observed.update(json.loads(completed.stdout.decode("utf-8")))
            return battery.BoundedProcessResult(
                returncode=0,
                stdout=b"{}",
                stderr=b"",
                stdout_truncated=False,
                stderr_truncated=False,
                timed_out=False,
            )

        monkeypatch.setattr(battery, "_run_worker_bounded", run_probe)
        environment = {
            "FRIDAY_LLM_BASE_URL": endpoint,
            "FRIDAY_EMBEDDINGS_BASE_URL": endpoint,
            "FRIDAY_RERANK_BASE_URL": endpoint,
        }
        with battery.SubprocessPassExecutor(environment) as executor:
            monkeypatch.setattr(executor, "_assert_candidate_unchanged", lambda: None)
            assert executor(manifest, manifest["passes"][0], cases, context) == {}

    assert observed == {
        "valid_request": True,
        "root": "/workspace",
        "production_imports": True,
        "direct_blocked": True,
        "relay_reply": "gnip-citehtnys",
        "guarded_reply": "gnip-dedraug",
        "guard_allowed": 1,
        "snapshot_read_only": True,
        "host_candidate_hidden": True,
        "sys_hidden": True,
    }
