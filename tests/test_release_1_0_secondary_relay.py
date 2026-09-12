"""Model-free proofs for the opt-in secondary endpoint relay seam."""

from __future__ import annotations

import contextlib
import socket
import stat
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
    def __init__(self, marker: bytes) -> None:
        self._marker = marker
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(8)
        self._listener.settimeout(0.1)
        self.port = int(self._listener.getsockname()[1])
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def __enter__(self) -> _TcpEcho:
        self._thread.start()
        return self

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                connection, _address = self._listener.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            with connection, contextlib.suppress(OSError):
                payload = connection.recv(1024)
                connection.sendall(self._marker + payload)

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        del exc_type, exc, traceback
        self._stop.set()
        self._listener.close()
        self._thread.join(timeout=2)


def _closed_endpoints() -> dict[str, str]:
    return {
        "model": "http://127.0.0.1:18001/v1",
        "embedding": "http://127.0.0.1:18002/v1",
        "reranker": "http://127.0.0.1:18003/v1",
        "secondary": "http://127.0.0.1:18004/v1",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("include_secondary", [False, True], ids=["default", "secondary"])
async def test_http_probe_secondary_opt_in_counts_inference_health_and_privacy(
    include_secondary: bool,
) -> None:
    import httpx

    endpoints = _closed_endpoints()
    settings = SimpleNamespace(
        llm_base_url=endpoints["model"],
        embeddings_base_url=endpoints["embedding"],
        rerank_base_url=endpoints["reranker"],
    )
    foreign = "SYN-SECONDARY-HTTP-PROBE-PRIVATE"
    if include_secondary:
        settings.secondary_llm_base_url = endpoints["secondary"]
        probe = battery.LocalEndpointHttpProbe(settings, [foreign], include_secondary=True)
    else:
        # The default contract does not require or consult secondary settings.
        probe = battery.LocalEndpointHttpProbe(settings, [foreign])
    original_send = httpx.AsyncClient.send
    forwarded: list[tuple[str, str]] = []

    async def handle(request: httpx.Request) -> httpx.Response:
        forwarded.append((request.method, request.url.path))
        return httpx.Response(204)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        probe.install()
        try:
            requests = (
                httpx.Request("POST", f"{endpoints['model']}/chat/completions", json={"text": "clean"}),
                httpx.Request(
                    "POST",
                    f"{endpoints['embedding']}/embeddings",
                    headers={"x-private": foreign},
                ),
                httpx.Request("POST", f"{endpoints['reranker']}/rerank", json={"text": foreign}),
                httpx.Request(
                    "POST",
                    f"{endpoints['secondary']}/chat/completions?q=%53YN-SECONDARY-HTTP-PROBE-PRIVATE",
                    json={"text": foreign},
                ),
                httpx.Request("POST", f"{endpoints['secondary']}/chat/completions", json={"text": "clean"}),
                httpx.Request("GET", f"{endpoints['model']}/status", params={"q": foreign}),
            )
            for request in requests:
                assert (await client.send(request)).status_code == 204
            if include_secondary:
                assert probe.counts["secondary"] == 2
                assert probe.counts["secondary_health"] == 0
            assert (
                await client.get(f"{endpoints['secondary']}/models", headers={"x-private": foreign})
            ).status_code == 204
            if include_secondary:
                assert probe.counts["secondary"] == 2
                assert probe.counts["secondary_health"] == 1
                assert probe.foreign_canary_sends["secondary_health"] == 1
            assert (
                await client.get(f"{endpoints['secondary']}/friday-profile", headers={"x-private": foreign})
            ).status_code == 204
            if include_secondary:
                assert probe.counts["secondary"] == 2
                assert probe.counts["secondary_health"] == 2
                assert probe.foreign_canary_sends["secondary_health"] == 2
            assert (await client.get(f"{endpoints['secondary']}/friday-profile/extra")).status_code == 204

            if include_secondary:
                assert probe.counts == {
                    "model": 1,
                    "embedding": 1,
                    "reranker": 1,
                    "other": 2,
                    "secondary": 2,
                    "secondary_health": 2,
                }
                assert probe.foreign_canary_sends == {
                    "model": 0,
                    "embedding": 1,
                    "reranker": 1,
                    "other": 1,
                    "secondary": 1,
                    "secondary_health": 2,
                }
            else:
                assert probe.counts == {"model": 1, "embedding": 1, "reranker": 1, "other": 6}
                assert probe.foreign_canary_sends == {
                    "model": 0,
                    "embedding": 1,
                    "reranker": 1,
                    "other": 4,
                }
            assert probe.foreign_canary_surfaces == {"url": 2, "headers": 3, "body": 2}
            assert probe.scan_failures == 0

            wrong_methods = (
                ("GET", f"{endpoints['model']}/chat/completions"),
                ("GET", f"{endpoints['embedding']}/embeddings"),
                ("GET", f"{endpoints['reranker']}/rerank"),
                ("GET", f"{endpoints['secondary']}/chat/completions"),
                ("POST", f"{endpoints['secondary']}/models"),
                ("POST", f"{endpoints['secondary']}/friday-profile"),
            )
            for method, url in wrong_methods:
                request = httpx.Request(
                    method,
                    url,
                    params={"q": foreign},
                    headers={"x-private": foreign},
                    json={"text": foreign},
                )
                assert (await client.send(request)).status_code == 204
            if include_secondary:
                assert probe.counts == {
                    "model": 1,
                    "embedding": 1,
                    "reranker": 1,
                    "other": 8,
                    "secondary": 2,
                    "secondary_health": 2,
                }
                assert probe.foreign_canary_sends == {
                    "model": 0,
                    "embedding": 1,
                    "reranker": 1,
                    "other": 7,
                    "secondary": 1,
                    "secondary_health": 2,
                }
            else:
                assert probe.counts == {"model": 1, "embedding": 1, "reranker": 1, "other": 12}
                assert probe.foreign_canary_sends == {
                    "model": 0,
                    "embedding": 1,
                    "reranker": 1,
                    "other": 10,
                }
            assert probe.foreign_canary_surfaces == {"url": 8, "headers": 9, "body": 8}
            assert probe.scan_failures == 0
        finally:
            probe.restore()
        assert httpx.AsyncClient.send is original_send
        counts = dict(probe.counts)
        privacy_counts = dict(probe.foreign_canary_sends)
        assert (
            await client.get(f"{endpoints['secondary']}/friday-profile", headers={"x-private": foreign})
        ).status_code == 204
        assert probe.counts == counts
        assert probe.foreign_canary_sends == privacy_counts
        assert probe.foreign_canary_surfaces == {"url": 8, "headers": 9, "body": 8}
    assert forwarded == [
        ("POST", "/v1/chat/completions"),
        ("POST", "/v1/embeddings"),
        ("POST", "/v1/rerank"),
        ("POST", "/v1/chat/completions"),
        ("POST", "/v1/chat/completions"),
        ("GET", "/v1/status"),
        ("GET", "/v1/models"),
        ("GET", "/v1/friday-profile"),
        ("GET", "/v1/friday-profile/extra"),
        ("GET", "/v1/chat/completions"),
        ("GET", "/v1/embeddings"),
        ("GET", "/v1/rerank"),
        ("GET", "/v1/chat/completions"),
        ("POST", "/v1/models"),
        ("POST", "/v1/friday-profile"),
        ("GET", "/v1/friday-profile"),
    ]


def test_http_probe_refuses_invalid_secondary_opt_in_and_normalized_target_collision() -> None:
    import httpx

    endpoints = _closed_endpoints()
    settings = SimpleNamespace(
        llm_base_url=endpoints["model"],
        embeddings_base_url=endpoints["embedding"],
        rerank_base_url=endpoints["reranker"],
    )
    original_send = httpx.AsyncClient.send
    for invalid in (None, 0, 1, "true", [], object()):
        with pytest.raises(battery.BatteryContractError, match="^http_probe_secondary_opt_in_invalid$"):
            battery.LocalEndpointHttpProbe(settings, include_secondary=invalid)
        assert httpx.AsyncClient.send is original_send
    with pytest.raises(TypeError):
        battery.LocalEndpointHttpProbe(settings, (), True)
    assert httpx.AsyncClient.send is original_send

    settings.llm_base_url = "http://127.0.0.1/v1/"
    settings.secondary_llm_base_url = "HTTP://127.0.0.1:80/v1"
    with pytest.raises(battery.BatteryContractError, match="^http_probe_endpoint_collision$"):
        battery.LocalEndpointHttpProbe(settings, include_secondary=True)
    assert httpx.AsyncClient.send is original_send
    default_probe = battery.LocalEndpointHttpProbe(settings, include_secondary=False)
    assert default_probe.counts == {"model": 0, "embedding": 0, "reranker": 0, "other": 0}
    assert default_probe.foreign_canary_sends == {"model": 0, "embedding": 0, "reranker": 0, "other": 0}
    assert httpx.AsyncClient.send is original_send

    settings.secondary_llm_base_url = "http://127.0.0.1:18004/v1#collapsed-targets"
    with pytest.raises(battery.BatteryContractError, match="^http_probe_endpoint_collision$"):
        battery.LocalEndpointHttpProbe(settings, include_secondary=True)
    assert httpx.AsyncClient.send is original_send


def test_secondary_endpoint_round_trips_through_loopback_and_uds_and_cleans_up() -> None:
    endpoints: dict[str, str] = {}
    kinds = ("model", "embedding", "reranker", "secondary")
    with contextlib.ExitStack() as stack:
        for kind in kinds:
            upstream = stack.enter_context(_TcpEcho(f"{kind}:".encode()))
            endpoints[kind] = f"http://127.0.0.1:{upstream.port}/v1"

        with battery._HostEndpointRelays(
            endpoints,
            include_secondary=True,
        ) as host_relays:
            assert host_relays.directory is not None
            relay_root = host_relays.directory
            assert stat.S_IMODE(relay_root.stat().st_mode) == 0o700
            assert {path.name for path in relay_root.iterdir()} == {
                "model.sock",
                "embedding.sock",
                "reranker.sock",
                "secondary.sock",
            }
            assert all(
                stat.S_ISSOCK(path.lstat().st_mode) and stat.S_IMODE(path.lstat().st_mode) == 0o600
                for path in relay_root.iterdir()
            )

            settings = SimpleNamespace(
                llm_base_url=endpoints["model"],
                embeddings_base_url=endpoints["embedding"],
                rerank_base_url=endpoints["reranker"],
                secondary_llm_base_url=endpoints["secondary"],
            )
            with battery._UnixRelayLoopbackBridge.from_settings(
                settings,
                relay_root,
                include_secondary=True,
            ) as bridge:
                secondary_target = battery._resolved_endpoint_targets(endpoints["secondary"])[0]
                canonical = battery._canonical_endpoint_target(*secondary_target)
                loopback_address = bridge.routes[canonical]
                assert len(bridge.routes) == 4
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as client:
                    client.settimeout(2)
                    client.connect(loopback_address)
                    client.sendall(b"model-free-ping")
                    assert client.recv(1024) == b"secondary:model-free-ping"

            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                probe.settimeout(0.2)
                with pytest.raises(OSError):
                    probe.connect(loopback_address)

        assert not relay_root.exists()


def test_secondary_requires_explicit_complete_closed_endpoint_set(tmp_path: Path) -> None:
    endpoints = _closed_endpoints()
    canonical = {kind: endpoints[kind] for kind in ("model", "embedding", "reranker")}

    default_relays = battery._HostEndpointRelays(canonical)
    assert default_relays.directory is None
    with pytest.raises(battery.BatteryContractError, match="worker_relay_endpoint_invalid"):
        battery._HostEndpointRelays(endpoints)
    with pytest.raises(battery.BatteryContractError, match="worker_relay_endpoint_invalid"):
        battery._HostEndpointRelays(canonical, include_secondary=True)

    unknown = dict(endpoints)
    unknown["shadow"] = unknown.pop("secondary")
    with pytest.raises(battery.BatteryContractError, match="worker_relay_endpoint_invalid"):
        battery._HostEndpointRelays(unknown, include_secondary=True)

    missing_root = tmp_path / "not-created"
    with pytest.raises(battery.BatteryContractError, match="worker_relay_endpoint_invalid"):
        battery._UnixRelayLoopbackBridge(
            unknown,
            missing_root,
            include_secondary=True,
        )
    with pytest.raises(battery.BatteryContractError, match="worker_relay_endpoint_invalid"):
        battery._UnixRelayLoopbackBridge(endpoints, missing_root)
    with pytest.raises(battery.BatteryContractError, match="worker_relay_endpoint_invalid"):
        battery._UnixRelayLoopbackBridge(
            canonical,
            missing_root,
            include_secondary=True,
        )
    with pytest.raises(battery.BatteryContractError, match="worker_relay_mount_invalid"):
        battery._UnixRelayLoopbackBridge(canonical, missing_root)
    with pytest.raises(battery.BatteryContractError, match="worker_relay_mount_invalid"):
        battery._UnixRelayLoopbackBridge(
            endpoints,
            missing_root,
            include_secondary=True,
        )


@pytest.mark.parametrize(
    "endpoint",
    (
        "http://localhost:18004/v1",
        "http://8.8.8.8:18004/v1",
        "http://127.0.0.1:0/v1",
        "http://127.0.0.1:not-a-port/v1",
    ),
)
def test_secondary_endpoint_must_be_numeric_local(endpoint: str, tmp_path: Path) -> None:
    endpoints = _closed_endpoints()
    endpoints["secondary"] = endpoint

    with pytest.raises(battery.BatteryContractError, match="worker_relay_endpoint_invalid"):
        battery._HostEndpointRelays(endpoints, include_secondary=True)
    with pytest.raises(battery.BatteryContractError, match="worker_relay_endpoint_invalid"):
        battery._UnixRelayLoopbackBridge(
            endpoints,
            tmp_path / "missing",
            include_secondary=True,
        )


def test_missing_secondary_socket_fails_before_loopback_listener(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    relay_root = tmp_path / "relays"
    relay_root.mkdir(mode=0o700)
    unix_listeners: list[socket.socket] = []
    for name in battery._RELAY_SOCKET_NAMES.values():
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(relay_root / name))
        (relay_root / name).chmod(0o600)
        listener.listen(1)
        unix_listeners.append(listener)

    real_socket = socket.socket

    class _UnexpectedListener(real_socket):
        def __new__(cls, *args: Any, **kwargs: Any) -> _UnexpectedListener:
            del cls, args, kwargs
            raise AssertionError("loopback listener created before relay preflight")

    try:
        monkeypatch.setattr(battery.socket, "socket", _UnexpectedListener)
        with pytest.raises(
            battery.BatteryContractError,
            match="worker_relay_socket_invalid",
        ):
            battery._UnixRelayLoopbackBridge(
                _closed_endpoints(),
                relay_root,
                include_secondary=True,
            )
    finally:
        for listener in unix_listeners:
            listener.close()


@pytest.mark.parametrize("fault", ["after_start", "thread_start"])
def test_partial_host_start_failure_closes_every_created_relay(monkeypatch, fault):
    relays = []
    original_start = battery._UnixToTcpEndpointRelay.start
    original_thread_start = threading.Thread.start

    def start(relay):
        relays.append(relay)
        original_start(relay)
        if fault == "after_start" and len(relays) == 4:
            raise RuntimeError("injected relay start failure")

    def start_thread(thread):
        if fault == "thread_start" and len(relays) == 4:
            raise RuntimeError("injected relay start failure")
        original_thread_start(thread)

    monkeypatch.setattr(battery._UnixToTcpEndpointRelay, "start", start)
    monkeypatch.setattr(threading.Thread, "start", start_thread)
    try:
        with (
            pytest.raises(RuntimeError, match="^injected relay start failure$"),
            battery._HostEndpointRelays(_closed_endpoints(), include_secondary=True),
        ):
            pytest.fail("partial start unexpectedly succeeded")
        assert len(relays) == 4
        assert all(relay._listener is None for relay in relays), "partially started relay escaped cleanup"
        assert all(not thread.is_alive() for relay in relays for thread in relay._threads)
    finally:
        # The negative control must not itself leave the reproduced listener
        # or thread alive. Remove only never-started synthetic thread entries.
        for relay in relays:
            relay._threads[:] = [thread for thread in relay._threads if thread.ident is not None]
            relay.stop()


@pytest.mark.parametrize("fault", ["after_start", "thread_start"])
def test_partial_worker_bridge_failure_closes_all_listeners_and_threads(monkeypatch, fault):
    owners = []
    original_spawn = battery._RelayLifecycle._spawn
    original_thread_start = threading.Thread.start
    starts = 0

    def spawn(owner, *args):
        owners.append(owner)
        original_spawn(owner, *args)

    def start_thread(thread):
        nonlocal starts
        starts += 1
        if starts == 4 and fault == "thread_start":
            raise RuntimeError("injected bridge thread failure")
        original_thread_start(thread)
        if starts == 4 and fault == "after_start":
            raise RuntimeError("injected bridge thread failure")

    with battery._HostEndpointRelays(_closed_endpoints(), include_secondary=True) as host:
        monkeypatch.setattr(battery._RelayLifecycle, "_spawn", spawn)
        monkeypatch.setattr(threading.Thread, "start", start_thread)
        with pytest.raises(RuntimeError, match="^injected bridge thread failure$"):
            battery._UnixRelayLoopbackBridge(_closed_endpoints(), host.directory, include_secondary=True)
        assert starts == 4
        bridge = owners[0]
        assert not bridge._listeners
        assert all(connection.fileno() == -1 for connection in bridge._connections)
        assert all(not thread.is_alive() for thread in bridge._threads)


def test_separate_guarded_worker_routes_four_http_services_and_denies_other_destination():
    import json
    import subprocess
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            payload = self.server.marker
            self.send_response(200)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *_args):
            pass

    worker = r"""
import json,sys,urllib.request
from pathlib import Path
sys.path.insert(0,str(Path(sys.argv[1])/'tools'))
import synthetic_live_battery as b
endpoints=json.loads(sys.argv[2])
with b._UnixRelayLoopbackBridge(endpoints,Path(sys.argv[3]),include_secondary=True) as bridge:
 with b.LocalEndpointNetworkGuard(list(endpoints.values()),relay_routes=bridge.routes) as guard:
  opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
  observed={}
  for kind,url in endpoints.items():
   with opener.open(url,timeout=2) as response:
    observed[kind]=response.read(128).decode()
  try:
   opener.open('http://127.0.0.1:1/forbidden',timeout=0.1)
  except OSError: pass
  observed['denied']=guard.denied_attempts
  observed['allowed']=guard.allowed_attempts
print(json.dumps(observed,sort_keys=True))
"""
    endpoints = {}
    with contextlib.ExitStack() as stack:
        for kind in ("model", "embedding", "reranker", "secondary"):
            server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            server.marker = kind.encode()
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            stack.callback(thread.join, 2)
            stack.callback(server.server_close)
            stack.callback(server.shutdown)
            endpoints[kind] = f"http://127.0.0.1:{server.server_port}/v1"
        with battery._HostEndpointRelays(endpoints, include_secondary=True) as host:
            child = subprocess.run(
                [
                    sys.executable,
                    "-I",
                    "-B",
                    "-c",
                    worker,
                    str(ROOT),
                    json.dumps(endpoints),
                    str(host.directory),
                ],
                env={},
                capture_output=True,
                timeout=15,
                check=False,
            )
    assert child.returncode == 0, child.stderr.decode()
    observed = json.loads(child.stdout)
    assert observed == {
        "model": "model",
        "embedding": "embedding",
        "reranker": "reranker",
        "secondary": "secondary",
        "denied": 1,
        "allowed": 4,
    }
