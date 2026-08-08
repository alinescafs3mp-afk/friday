"""Proof regressions for the live battery's process-local network boundary."""

from __future__ import annotations

import socket
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import synthetic_live_battery as battery  # noqa: E402


def _tcp_endpoint() -> tuple[socket.socket, int]:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    return listener, int(listener.getsockname()[1])


def test_allowed_tuple_is_tcp_stream_only() -> None:
    listener, port = _tcp_endpoint()
    guard = battery.LocalEndpointNetworkGuard([f"http://127.0.0.1:{port}"])
    try:
        with guard:
            resolved = socket.getaddrinfo("127.0.0.1", port)
            assert resolved
            assert all(
                family in {socket.AF_INET, socket.AF_INET6}
                and int(sock_type) & 0xF == socket.SOCK_STREAM
                and protocol in {0, socket.IPPROTO_TCP}
                for family, sock_type, protocol, _canonical_name, _address in resolved
            )

            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
                with pytest.raises(PermissionError):
                    udp.connect(("127.0.0.1", port))
                with pytest.raises(PermissionError):
                    udp.sendto(b"probe", ("127.0.0.1", port))
                if hasattr(udp, "sendmsg"):
                    with pytest.raises(PermissionError):
                        udp.sendmsg([b"probe"], [], 0, ("127.0.0.1", port))

            with pytest.raises(PermissionError):
                socket.getaddrinfo("127.0.0.1", port, type=socket.SOCK_DGRAM)
    finally:
        listener.close()


def test_raw_socket_kind_is_rejected_without_requiring_raw_socket_privileges() -> None:
    listener, port = _tcp_endpoint()
    guard = battery.LocalEndpointNetworkGuard([f"http://127.0.0.1:{port}"])
    fake_raw_socket = SimpleNamespace(family=socket.AF_INET, type=socket.SOCK_RAW)
    try:
        with pytest.raises(PermissionError):
            guard._require_http_stream_socket(fake_raw_socket)
        assert guard.denied_attempts == 1
    finally:
        listener.close()


def test_internet_bind_listen_and_accept_fail_closed() -> None:
    listener, port = _tcp_endpoint()
    guard = battery.LocalEndpointNetworkGuard([f"http://127.0.0.1:{port}"])
    try:
        with guard:
            with (
                socket.socket(socket.AF_INET, socket.SOCK_STREAM) as candidate,
                pytest.raises(PermissionError),
            ):
                candidate.bind(("127.0.0.1", 0))
            with pytest.raises(PermissionError):
                listener.listen(1)
            with pytest.raises(PermissionError):
                listener.accept()
        assert guard.denied_attempts == 3
    finally:
        listener.close()


def test_ipv6_bind_fails_closed_when_ipv6_sockets_are_available() -> None:
    listener, port = _tcp_endpoint()
    guard = battery.LocalEndpointNetworkGuard([f"http://127.0.0.1:{port}"])
    try:
        try:
            candidate = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        except OSError:
            pytest.skip("IPv6 sockets are unavailable")
        with candidate, guard, pytest.raises(PermissionError):
            candidate.bind(("::1", 0))
    finally:
        listener.close()


@pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="AF_UNIX is unavailable")
def test_listener_guard_does_not_expand_to_unix_sockets(tmp_path: Path) -> None:
    listener, port = _tcp_endpoint()
    guard = battery.LocalEndpointNetworkGuard([f"http://127.0.0.1:{port}"])
    unix_listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    unix_client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    socket_path = str(tmp_path / "network-guard.sock")
    try:
        with guard:
            unix_listener.bind(socket_path)
            unix_listener.listen(1)
            guard._original_connect(unix_client, socket_path)
            accepted, _address = unix_listener.accept()
            accepted.close()
    finally:
        unix_client.close()
        unix_listener.close()
        listener.close()
