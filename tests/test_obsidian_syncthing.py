from __future__ import annotations

import json
import os
import socket
import socketserver
import subprocess
import threading
from collections.abc import Mapping, Sequence
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from friday.organs.obsidian.syncthing import (
    MAX_CONFIG_BYTES,
    RELAY_LISTEN_ADDRESSES,
    CommandResult,
    DeviceConnection,
    LoopbackHTTPTransport,
    SyncthingConfigurationError,
    SyncthingConnectivityPolicyError,
    SyncthingFileInfo,
    SyncthingFileStatus,
    SyncthingHTTPError,
    SyncthingProcessError,
    SyncthingProcessExitedError,
    SyncthingProcessManager,
    SyncthingProcessSupervisor,
    SyncthingProfileLimitError,
    SyncthingProtocolError,
    SyncthingReadiness,
    SyncthingReadinessTimeoutError,
    SyncthingResponseTooLargeError,
    SyncthingRestClient,
    SyncthingSecurityError,
    SyncthingSystemStatus,
    SyncthingTransportError,
    SyncthingVersion,
    TransportResponse,
    UnixSocketTransport,
    build_generate_command,
    build_serve_command,
    owner_filesystem_key,
    prepare_profile_directories,
    read_api_key,
)
from friday.organs.obsidian.syncthing import SyncthingProfileSpec as ProfileSpec

DEVICE_A = "DOVII4U-SQEEESM-VZ2CVTC-CJM4YN5-QNV7DCU-5U3ASRL-YVFG6TH-W5DV5AA"
DEVICE_B = "YZJBJFX-RDBL7WY-6ZGKJ2D-4MJB4E7-ZATSDUY-LD6Y3L3-MLFUYWE-AEMXJAC"


def json_response(payload: object, *, status: int = 200) -> TransportResponse:
    return TransportResponse(
        status=status,
        headers={"content-type": "application/json; charset=utf-8"},
        body=json.dumps(payload).encode(),
    )


class RecordingTransport:
    def __init__(self, *responses: TransportResponse) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, object]] = []

    def request(
        self,
        method: str,
        target: str,
        *,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout: float,
        max_response_bytes: int,
    ) -> TransportResponse:
        self.calls.append(
            {
                "method": method,
                "target": target,
                "headers": dict(headers),
                "body": body,
                "timeout": timeout,
                "max_response_bytes": max_response_bytes,
            }
        )
        return self.responses.pop(0)


def test_rest_client_authenticates_and_normalizes_status_and_version() -> None:
    transport = RecordingTransport(
        json_response({"myID": DEVICE_A, "guiAddressUsed": "127.0.0.1:8384", "uptime": 42}),
        json_response(
            {"version": "v2.0.3", "longVersion": "syncthing v2.0.3", "os": "linux", "arch": "amd64"}
        ),
    )
    client = SyncthingRestClient(transport, "secret-api-key", timeout=1.25)

    assert client.system_status() == SyncthingSystemStatus(DEVICE_A, "127.0.0.1:8384", 42)
    assert client.system_version() == SyncthingVersion("v2.0.3", "syncthing v2.0.3", "linux", "amd64")
    assert [call["target"] for call in transport.calls] == [
        "/rest/system/status",
        "/rest/system/version",
    ]
    assert all(call["headers"]["X-API-Key"] == "secret-api-key" for call in transport.calls)
    assert all(call["timeout"] == 1.25 for call in transport.calls)
    assert "secret-api-key" not in repr(client)


def test_pending_device_and_device_configuration_endpoints_are_exact() -> None:
    transport = RecordingTransport(
        json_response(
            {
                DEVICE_B: {
                    "name": "Pixel",
                    "address": "tcp://192.0.2.3:22000",
                    "time": "2026-08-21T12:00:00Z",
                },
                DEVICE_A: {"name": "Tablet"},
            }
        ),
        TransportResponse(200, {}, b""),
        json_response([{"deviceID": DEVICE_A, "name": "Android", "addresses": ["dynamic"], "paused": False}]),
        json_response({"deviceID": DEVICE_A, "name": "Android", "addresses": [], "paused": True}),
        TransportResponse(200, {}, b""),
        TransportResponse(200, {}, b""),
        TransportResponse(200, {}, b""),
    )
    client = SyncthingRestClient(transport, "key")

    pending = client.list_pending_devices()
    assert [item.device_id for item in pending] == [DEVICE_A, DEVICE_B]
    assert pending[1].name == "Pixel"
    client.delete_pending_device(DEVICE_B)
    assert client.list_devices()[0].addresses == ("dynamic",)
    assert client.get_device(DEVICE_A).paused is True
    client.post_device({"deviceID": DEVICE_B, "name": "Phone"})
    client.patch_device(DEVICE_B, {"paused": True})
    client.delete_device(DEVICE_B)

    assert [(call["method"], call["target"]) for call in transport.calls] == [
        ("GET", "/rest/cluster/pending/devices"),
        ("DELETE", f"/rest/cluster/pending/devices?device={DEVICE_B}"),
        ("GET", "/rest/config/devices"),
        ("GET", f"/rest/config/devices/{DEVICE_A}"),
        ("POST", "/rest/config/devices"),
        ("PATCH", f"/rest/config/devices/{DEVICE_B}"),
        ("DELETE", f"/rest/config/devices/{DEVICE_B}"),
    ]
    assert json.loads(transport.calls[4]["body"]) == {"deviceID": DEVICE_B, "name": "Phone"}
    assert transport.calls[4]["headers"]["Content-Type"] == "application/json"


def test_folder_scan_status_and_completion_are_normalized() -> None:
    transport = RecordingTransport(
        json_response(
            [
                {
                    "id": "vault one",
                    "label": "Friday",
                    "path": "/private/vault",
                    "devices": [{"deviceID": DEVICE_A}],
                    "paused": False,
                    "type": "sendreceive",
                    "versioning": {
                        "type": "staggered",
                        "params": {"cleanoutDays": "365", "maxAge": "31536000"},
                        "cleanupIntervalS": 3600,
                        "fsPath": "",
                        "fsType": "basic",
                    },
                }
            ]
        ),
        json_response(
            {
                "id": "vault one",
                "label": "Friday",
                "path": "/private/vault",
                "devices": [],
            }
        ),
        TransportResponse(200, {}, b""),
        TransportResponse(200, {}, b""),
        TransportResponse(200, {}, b""),
        TransportResponse(200, {}, b""),
        json_response(
            {
                "state": "idle",
                "stateChanged": "2026-08-21T12:00:00Z",
                "globalFiles": 8,
                "localFiles": 8,
                "needFiles": 0,
                "needBytes": 0,
            }
        ),
        json_response(
            {
                "completion": 100,
                "needBytes": 0,
                "needItems": 0,
                "globalBytes": 120,
                "globalItems": 8,
                "remoteState": "valid",
            }
        ),
        json_response({"requiresRestart": False}),
    )
    client = SyncthingRestClient(transport, "key")

    folder = client.list_folders()[0]
    assert folder.folder_id == "vault one"
    assert folder.device_ids == (DEVICE_A,)
    assert folder.versioning_type == "staggered"
    assert dict(folder.versioning_params) == {"cleanoutDays": "365", "maxAge": "31536000"}
    assert folder.versioning_cleanup_interval_s == 3600
    assert folder.versioning_fs_path == ""
    assert folder.versioning_fs_type == "basic"
    assert client.get_folder("vault one").device_ids == ()
    client.post_folder({"id": "vault one", "path": "/private/vault"})
    client.patch_folder("vault one", {"paused": True})
    client.delete_folder("vault one")
    client.scan_folder("vault one", subpath="Notes/day.md")
    status = client.folder_status("vault one")
    completion = client.remote_completion("vault one", DEVICE_A)

    assert status.state == "idle" and status.need_files == 0
    assert completion.is_complete and completion.remote_state == "valid"
    assert client.restart_required() is False
    assert transport.calls[1]["target"] == "/rest/config/folders/vault%20one"
    assert transport.calls[5]["target"] == "/rest/db/scan?folder=vault+one&sub=Notes%2Fday.md"
    assert transport.calls[7]["target"].endswith(f"device={DEVICE_A}")


def test_connections_and_exact_file_availability_are_typed() -> None:
    file_entry = {
        "name": "Notes/round-trip.md",
        "size": 120,
        "modified": "2026-08-21T12:00:00Z",
        "deleted": False,
        "ignored": False,
        "invalid": False,
        "mustRescan": False,
        "noPermissions": False,
        "type": "FILE_INFO_TYPE_FILE",
        "version": ["AAAAAAA:7"],
    }
    transport = RecordingTransport(
        json_response(
            {
                "connections": {
                    DEVICE_B: {"connected": False, "paused": False},
                    DEVICE_A: {
                        "connected": True,
                        "paused": False,
                        "address": "relay://198.51.100.2:22067",
                        "type": "relay-client",
                        "clientVersion": "syncthing v2.0.3",
                        "at": "2026-08-21T12:00:01Z",
                        "startedAt": "2026-08-21T11:59:00Z",
                        "isLocal": False,
                        "inBytesTotal": 10,
                        "outBytesTotal": 20,
                    },
                }
            }
        ),
        json_response(
            {
                "local": file_entry,
                "global": dict(file_entry),
                "availability": [
                    {"id": DEVICE_B, "fromTemporary": True},
                    {"id": DEVICE_A, "fromTemporary": False},
                ],
            }
        ),
    )
    client = SyncthingRestClient(transport, "key")

    connections = client.connections()
    assert connections[0] == DeviceConnection(
        DEVICE_A,
        True,
        False,
        "relay://198.51.100.2:22067",
        "relay-client",
        "syncthing v2.0.3",
        "2026-08-21T12:00:01Z",
        "2026-08-21T11:59:00Z",
        False,
        10,
        20,
    )
    assert connections[0].via_relay
    assert not connections[1].connected

    status = client.file_status("vault one", "Notes/round-trip.md")
    assert isinstance(status, SyncthingFileStatus)
    assert isinstance(status.local, SyncthingFileInfo)
    assert status.local_matches_global
    assert status.available_on(DEVICE_A)
    assert not status.available_on(DEVICE_B)
    assert status.available_on(DEVICE_B, include_temporary=True)
    assert not replace(status, local=replace(status.local, must_rescan=True)).local_matches_global
    assert transport.calls[0]["target"] == "/rest/system/connections"
    assert transport.calls[1]["target"] == ("/rest/db/file?folder=vault+one&file=Notes%2Fround-trip.md")


def test_discovery_relay_options_are_patched_read_back_and_restart_checked() -> None:
    transport = RecordingTransport(
        TransportResponse(200, {}, b""),
        json_response(
            {
                "listenAddresses": list(RELAY_LISTEN_ADDRESSES),
                "localAnnounceEnabled": False,
                "globalAnnounceEnabled": True,
                "relaysEnabled": True,
            }
        ),
        json_response({"requiresRestart": True}),
    )
    client = SyncthingRestClient(transport, "key")

    applied = client.apply_discovery_relay()

    assert applied.options.is_discovery_relay
    assert applied.restart_required
    assert [(call["method"], call["target"]) for call in transport.calls] == [
        ("PATCH", "/rest/config/options"),
        ("GET", "/rest/config/options"),
        ("GET", "/rest/config/restart-required"),
    ]
    assert json.loads(transport.calls[0]["body"]) == {
        "listenAddresses": list(RELAY_LISTEN_ADDRESSES),
        "localAnnounceEnabled": False,
        "globalAnnounceEnabled": True,
        "relaysEnabled": True,
    }


def test_discovery_relay_configuration_fails_closed_on_readback_mismatch() -> None:
    transport = RecordingTransport(
        TransportResponse(200, {}, b""),
        json_response(
            {
                "listenAddresses": ["default"],
                "localAnnounceEnabled": False,
                "globalAnnounceEnabled": True,
                "relaysEnabled": True,
            }
        ),
    )
    with pytest.raises(SyncthingConnectivityPolicyError):
        SyncthingRestClient(transport, "key").apply_discovery_relay()

    with pytest.raises(SyncthingConfigurationError):
        SyncthingRestClient(RecordingTransport(), "key").patch_options({"natEnabled": False})


@pytest.mark.parametrize(
    "response",
    [
        TransportResponse(200, {"content-type": "application/json"}, b"not-json"),
        TransportResponse(200, {"content-type": "application/json"}, b'{"x":1,"x":2}'),
        TransportResponse(200, {"content-type": "text/html"}, b"{}"),
        TransportResponse(200, {"content-type": "application/json"}, b"[]"),
        TransportResponse(200, {"content-type": "application/json"}, b'{"myID":"bad"}'),
    ],
)
def test_status_rejects_invalid_json_contracts(response: TransportResponse) -> None:
    with pytest.raises(SyncthingProtocolError):
        SyncthingRestClient(RecordingTransport(response), "key").system_status()


def test_http_and_response_size_errors_are_typed_without_echoing_body() -> None:
    body = b"private response body"
    client = SyncthingRestClient(RecordingTransport(TransportResponse(401, {}, body)), "key")
    with pytest.raises(SyncthingHTTPError) as caught:
        client.system_status()
    assert caught.value.status == 401
    assert body.decode() not in str(caught.value)

    client = SyncthingRestClient(
        RecordingTransport(TransportResponse(200, {"content-type": "application/json"}, b"{}" * 10)),
        "key",
        max_response_bytes=3,
    )
    with pytest.raises(SyncthingResponseTooLargeError):
        client.system_status()


def test_client_rejects_secret_header_injection_and_unsafe_mutations() -> None:
    with pytest.raises(SyncthingConfigurationError):
        SyncthingRestClient(RecordingTransport(), "key\r\nX-Evil: yes")
    client = SyncthingRestClient(RecordingTransport(), "key")
    with pytest.raises(SyncthingConfigurationError):
        client.patch_device(DEVICE_A, {"deviceID": DEVICE_B})
    with pytest.raises(SyncthingConfigurationError):
        client.patch_folder("vault", {"id": "another"})
    with pytest.raises(SyncthingConfigurationError):
        client.scan_folder("vault", subpath="../outside")


class _HTTPHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler contract
        self.server.seen = (self.path, self.headers.get("X-API-Key"))  # type: ignore[attr-defined]
        payload = b'{"version":"v2.0.3"}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, _format: str, *args: object) -> None:
        return


def test_loopback_transport_talks_directly_to_local_http() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _HTTPHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        endpoint = f"http://127.0.0.1:{server.server_port}"
        client = SyncthingRestClient(LoopbackHTTPTransport(endpoint), "local-key")
        assert client.system_version().version == "v2.0.3"
        assert server.seen == ("/rest/system/version", "local-key")  # type: ignore[attr-defined]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://127.0.0.1:8384",
        "http://localhost:8384",
        "http://192.0.2.1:8384",
        "http://user:pass@127.0.0.1:8384",
        "http://127.0.0.1:8384/rest/system/status",
    ],
)
def test_loopback_transport_rejects_any_nonlocal_or_ambiguous_origin(endpoint: str) -> None:
    with pytest.raises((SyncthingConfigurationError, SyncthingSecurityError)):
        LoopbackHTTPTransport(endpoint)


class _UnixHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        request_line = self.rfile.readline().decode("ascii")
        while self.rfile.readline() not in {b"\r\n", b"\n", b""}:
            pass
        self.server.request_line = request_line  # type: ignore[attr-defined]
        payload = b'{"version":"v2.0.3"}'
        self.wfile.write(
            b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: "
            + str(len(payload)).encode()
            + b"\r\nConnection: close\r\n\r\n"
            + payload
        )


@pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="Unix sockets are unavailable")
def test_private_unix_transport_and_parent_permission_boundary(tmp_path: Path) -> None:
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    socket_path = private / "syncthing.sock"
    server = socketserver.UnixStreamServer(os.fspath(socket_path), _UnixHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        client = SyncthingRestClient(UnixSocketTransport(socket_path), "key")
        assert client.system_version().version == "v2.0.3"
        assert server.request_line.startswith("GET /rest/system/version ")  # type: ignore[attr-defined]
        private.chmod(0o755)
        with pytest.raises(SyncthingSecurityError):
            client.system_version()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_owner_identity_is_hashed_and_commands_are_key_free(tmp_path: Path) -> None:
    base = tmp_path / "o"
    first = ProfileSpec.for_owner(base, "actor-own-id", gui_mode="loopback", gui_port=18384)
    again = ProfileSpec.for_owner(base, "actor-own-id", gui_mode="loopback", gui_port=18384)
    other = ProfileSpec.for_owner(base, "shared-archive-id", gui_mode="loopback", gui_port=18385)

    assert first.owner_fs_key == again.owner_fs_key
    assert first.owner_fs_key != other.owner_fs_key
    assert first.owner_fs_key == owner_filesystem_key("actor-own-id")
    assert "actor-own-id" not in os.fspath(first.profile_root)
    assert first.config_root != first.data_root
    generate = build_generate_command(first)
    serve = build_serve_command(first)
    assert generate[1] == "generate" and serve[1] == "serve"
    assert not any(argument.startswith("--gui-address=") for argument in generate)
    assert f"--gui-address={first.gui_address}" in serve
    assert "--no-browser" in serve
    assert "--no-port-probing" in serve
    assert "--no-restart" in serve
    assert "--no-upgrade" in serve
    assert not any("apikey" in argument.lower() or "secret" in argument for argument in generate + serve)


def test_owner_and_profile_inputs_are_bounded(tmp_path: Path) -> None:
    with pytest.raises(SyncthingConfigurationError):
        ProfileSpec.for_owner("relative", "actor")
    with pytest.raises(SyncthingConfigurationError):
        ProfileSpec.for_owner(tmp_path, "")
    with pytest.raises(SyncthingConfigurationError):
        ProfileSpec.for_owner(tmp_path, "x" * 513)
    with pytest.raises(SyncthingConfigurationError):
        ProfileSpec.for_owner(tmp_path, "actor", gui_mode="loopback", gui_port=0)


def _write_config(path: Path, api_key: str = "generated-key") -> None:
    path.write_text(
        f"<configuration><gui><apikey>{api_key}</apikey></gui></configuration>",
        encoding="utf-8",
    )
    path.chmod(0o600)


def test_profile_directories_and_config_secret_are_private(tmp_path: Path) -> None:
    spec = ProfileSpec.for_owner(tmp_path / "obs", "actor", gui_mode="loopback", gui_port=18384)
    prepare_profile_directories(spec)
    for path in (
        spec.base_root,
        spec.base_root / "users",
        spec.profile_root,
        spec.config_root,
        spec.data_root,
        spec.vault_root,
        spec.runtime_root,
    ):
        assert path.stat().st_mode & 0o077 == 0
    _write_config(spec.config_file)
    assert read_api_key(spec.config_file) == "generated-key"

    spec.config_file.chmod(0o644)
    with pytest.raises(SyncthingSecurityError):
        read_api_key(spec.config_file)


def test_insecure_or_oversized_profile_material_is_rejected(tmp_path: Path) -> None:
    base = tmp_path / "obs"
    base.mkdir(mode=0o755)
    spec = ProfileSpec.for_owner(base, "actor", gui_mode="loopback", gui_port=18384)
    with pytest.raises(SyncthingSecurityError):
        prepare_profile_directories(spec)

    private = tmp_path / "private-config"
    private.mkdir(mode=0o700)
    config = private / "config.xml"
    config.write_bytes(b"x" * (MAX_CONFIG_BYTES + 1))
    config.chmod(0o600)
    with pytest.raises(SyncthingConfigurationError):
        read_api_key(config)

    config.write_text(
        '<!DOCTYPE x [<!ENTITY key "stolen">]><configuration><gui><apikey>&key;</apikey></gui></configuration>',
        encoding="utf-8",
    )
    config.chmod(0o600)
    with pytest.raises(SyncthingConfigurationError):
        read_api_key(config)


class FakeProcess:
    pid = 1234

    def __init__(self, *, returncode: int | None = None, hang_on_terminate: bool = False) -> None:
        self.returncode = returncode
        self.hang_on_terminate = hang_on_terminate
        self.terminated = False
        self.killed = False

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    def wait(self, timeout: float | None = None) -> int:
        if self.hang_on_terminate and not self.killed:
            raise subprocess.TimeoutExpired("syncthing", timeout)
        self.returncode = 0 if self.returncode is None else self.returncode
        return self.returncode


class FakeRunner:
    def __init__(self, spec: ProfileSpec, process: FakeProcess, *, generate_status: int = 0) -> None:
        self.spec = spec
        self.process = process
        self.generate_status = generate_status
        self.run_calls: list[tuple[str, ...]] = []
        self.spawn_calls: list[tuple[tuple[str, ...], Path, Path]] = []

    def run(self, argv: Sequence[str], *, timeout: float) -> CommandResult:
        self.run_calls.append(tuple(argv))
        if self.generate_status == 0:
            _write_config(self.spec.config_file)
        return CommandResult(self.generate_status)

    def spawn(self, argv: Sequence[str], *, cwd: Path, log_path: Path) -> FakeProcess:
        self.spawn_calls.append((tuple(argv), cwd, log_path))
        return self.process


class FakeClient:
    def __init__(self, failures: int = 0) -> None:
        self.failures = failures
        self.version_calls = 0

    def system_version(self) -> SyncthingVersion:
        self.version_calls += 1
        if self.version_calls <= self.failures:
            raise SyncthingTransportError("not ready")
        return SyncthingVersion("v2.0.3", None, "linux", "amd64")

    def system_status(self) -> SyncthingSystemStatus:
        return SyncthingSystemStatus(DEVICE_A, None, 1)


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, duration: float) -> None:
        self.now += duration


def test_supervisor_generates_starts_waits_and_stops_without_binary(tmp_path: Path) -> None:
    spec = ProfileSpec.for_owner(tmp_path / "obs", "actor", gui_mode="loopback", gui_port=18384)
    process = FakeProcess(hang_on_terminate=True)
    runner = FakeRunner(spec, process)
    client = FakeClient(failures=2)
    clock = FakeClock()
    observed_key: list[str] = []

    def factory(_spec: ProfileSpec, key: str) -> FakeClient:
        observed_key.append(key)
        return client

    supervisor = SyncthingProcessSupervisor(
        spec,
        runner=runner,
        client_factory=factory,  # type: ignore[arg-type]
        clock=clock,
        sleeper=clock.sleep,
    )
    ready = supervisor.start(readiness_timeout=2, poll_interval=0.25)

    assert ready.status.server_device_id == DEVICE_A
    assert observed_key == ["generated-key"]
    assert supervisor.is_running
    assert supervisor.client is client
    assert len(runner.run_calls) == 1 and len(runner.spawn_calls) == 1
    assert not any("generated-key" in argument for argument in runner.run_calls[0])
    assert not any("generated-key" in argument for argument in runner.spawn_calls[0][0])

    supervisor.stop(timeout=0.1)
    assert process.terminated and process.killed
    assert not supervisor.is_running
    supervisor.stop()


def test_supervisor_readiness_timeout_cleans_up_process(tmp_path: Path) -> None:
    spec = ProfileSpec.for_owner(tmp_path / "obs", "actor", gui_mode="loopback", gui_port=18384)
    process = FakeProcess()
    runner = FakeRunner(spec, process)
    client = FakeClient(failures=100)
    clock = FakeClock()
    supervisor = SyncthingProcessSupervisor(
        spec,
        runner=runner,
        client_factory=lambda _spec, _key: client,  # type: ignore[arg-type]
        clock=clock,
        sleeper=clock.sleep,
    )

    with pytest.raises(SyncthingReadinessTimeoutError):
        supervisor.start(readiness_timeout=0.5, poll_interval=0.25)
    assert process.terminated
    assert not supervisor.is_running


def test_supervisor_surfaces_generation_and_early_exit(tmp_path: Path) -> None:
    spec = ProfileSpec.for_owner(tmp_path / "first", "actor", gui_mode="loopback", gui_port=18384)
    runner = FakeRunner(spec, FakeProcess(), generate_status=9)
    supervisor = SyncthingProcessSupervisor(spec, runner=runner)
    with pytest.raises(SyncthingProcessError, match="status 9"):
        supervisor.start()

    spec = ProfileSpec.for_owner(tmp_path / "second", "actor", gui_mode="loopback", gui_port=18385)
    runner = FakeRunner(spec, FakeProcess(returncode=7))
    client = FakeClient()
    supervisor = SyncthingProcessSupervisor(
        spec,
        runner=runner,
        client_factory=lambda _spec, _key: client,  # type: ignore[arg-type]
    )
    with pytest.raises(SyncthingProcessExitedError) as caught:
        supervisor.start()
    assert caught.value.returncode == 7


def test_supervisor_does_not_regenerate_an_existing_profile(tmp_path: Path) -> None:
    spec = ProfileSpec.for_owner(tmp_path / "obs", "actor", gui_mode="loopback", gui_port=18384)
    prepare_profile_directories(spec)
    _write_config(spec.config_file, "persistent-key")
    process = FakeProcess()
    runner = FakeRunner(spec, process)
    client = FakeClient()
    supervisor = SyncthingProcessSupervisor(
        spec,
        runner=runner,
        client_factory=lambda _spec, key: client if key == "persistent-key" else None,  # type: ignore[arg-type,return-value]
    )

    supervisor.start()
    assert runner.run_calls == []
    supervisor.stop()


class StubSupervisor:
    def __init__(self, spec: ProfileSpec) -> None:
        self.spec = spec
        self.starts = 0
        self.stops = 0
        self._client = object()

    def start(self, *, readiness_timeout: float, poll_interval: float) -> object:
        self.starts += 1
        return SyncthingReadiness(
            SyncthingVersion("v2.0.3", None, "linux", "amd64"),
            SyncthingSystemStatus(DEVICE_A, None, 1),
        )

    @property
    def client(self) -> object:
        return self._client

    def stop(self, *, timeout: float) -> None:
        self.stops += 1


def test_process_manager_reuses_one_profile_and_exposes_its_client(tmp_path: Path) -> None:
    created: list[StubSupervisor] = []

    def factory(spec: ProfileSpec) -> StubSupervisor:
        supervisor = StubSupervisor(spec)
        created.append(supervisor)
        return supervisor

    manager = SyncthingProcessManager(supervisor_factory=factory)  # type: ignore[arg-type]
    spec = ProfileSpec.for_owner(
        tmp_path / "obs",
        "actor",
        profile_id="stprof_database_row",
        gui_mode="loopback",
        gui_port=18384,
    )

    first = manager.ensure_profile(spec)
    second = manager.ensure_profile(spec)

    assert first == second
    assert len(created) == 1 and created[0].starts == 2
    assert manager.client_for("stprof_database_row") is created[0].client
    assert manager.stop_profile("stprof_database_row")
    assert created[0].stops == 1
    assert not manager.stop_profile("stprof_database_row")
    with pytest.raises(SyncthingProcessError):
        manager.client_for("stprof_database_row")


def test_process_manager_rejects_duplicate_identity_endpoint_and_capacity(tmp_path: Path) -> None:
    manager = SyncthingProcessManager(
        max_profiles=1,
        supervisor_factory=StubSupervisor,  # type: ignore[arg-type]
    )
    first = ProfileSpec.for_owner(
        tmp_path / "obs",
        "actor-a",
        profile_id="profile-a",
        gui_mode="loopback",
        gui_port=18384,
    )
    manager.ensure_profile(first)

    changed = ProfileSpec.for_owner(
        tmp_path / "another",
        "actor-b",
        profile_id="profile-a",
        gui_mode="loopback",
        gui_port=18385,
    )
    with pytest.raises(SyncthingConfigurationError, match="different profile specification"):
        manager.ensure_profile(changed)

    second = ProfileSpec.for_owner(
        tmp_path / "obs",
        "actor-b",
        profile_id="profile-b",
        gui_mode="loopback",
        gui_port=18385,
    )
    with pytest.raises(SyncthingProfileLimitError):
        manager.ensure_profile(second)
    manager.close()
    with pytest.raises(SyncthingProcessError, match="closed"):
        manager.ensure_profile(first)


def test_process_manager_rejects_root_and_gui_collisions(tmp_path: Path) -> None:
    manager = SyncthingProcessManager(supervisor_factory=StubSupervisor)  # type: ignore[arg-type]
    first = ProfileSpec.for_owner(
        tmp_path / "obs",
        "actor",
        profile_id="profile-a",
        gui_mode="loopback",
        gui_port=18384,
    )
    manager.ensure_profile(first)

    same_root = ProfileSpec.for_owner(
        tmp_path / "obs",
        "actor",
        profile_id="profile-b",
        gui_mode="loopback",
        gui_port=18385,
    )
    with pytest.raises(SyncthingConfigurationError, match="root"):
        manager.ensure_profile(same_root)

    same_gui = ProfileSpec.for_owner(
        tmp_path / "obs",
        "other-actor",
        profile_id="profile-c",
        gui_mode="loopback",
        gui_port=18384,
    )
    with pytest.raises(SyncthingConfigurationError, match="GUI endpoint"):
        manager.ensure_profile(same_gui)
    manager.close()
