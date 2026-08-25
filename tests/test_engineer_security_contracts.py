"""Isolated security contracts for the engineer workbench.

The fixtures below use synthetic bytes and monkeypatched sockets only.  They
must never turn this contract suite into a live network probe.
"""

from __future__ import annotations

import hashlib
import io
import json
import stat
import struct
import warnings
import zipfile
from typing import Any

import pytest

from friday.organs.engineer import artifacts, hosts, local_binaries
from friday.organs.engineer.authority import (
    TargetTicketError,
    issue_target_ticket,
    verify_target_ticket,
)
from friday.organs.engineer.redaction import redact_header, redact_text, redact_url
from friday.organs.engineer.targets import PinnedTarget

_SIGNING_KEY = b"engineer-security-contract-key!!"
_SOURCE_SHA256 = hashlib.sha256(b"synthetic current user turn").hexdigest()


def _target(
    *,
    host: str = "service.example",
    addresses: tuple[str, ...] = ("127.0.0.1",),
) -> PinnedTarget:
    return PinnedTarget(
        host=host,
        addresses=addresses,
        implied_port=None,
        source_token=host,
        source_sha256=_SOURCE_SHA256,
    )


def _zip_with(info: str | zipfile.ZipInfo, payload: bytes, *, compression: int = zipfile.ZIP_STORED) -> bytes:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=compression) as archive:
        archive.writestr(info, payload)
    return stream.getvalue()


def _mark_zip_encrypted(data: bytes) -> bytes:
    """Set the encryption bit in local and central headers without decrypting."""

    mutated = bytearray(data)
    local = mutated.find(b"PK\x03\x04")
    central = mutated.find(b"PK\x01\x02")
    assert local >= 0 and central >= 0
    struct.pack_into("<H", mutated, local + 6, struct.unpack_from("<H", mutated, local + 6)[0] | 0x1)
    struct.pack_into(
        "<H",
        mutated,
        central + 8,
        struct.unpack_from("<H", mutated, central + 8)[0] | 0x1,
    )
    return bytes(mutated)


def _unsafe_zip_variants() -> dict[str, bytes]:
    duplicate = io.BytesIO()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        with zipfile.ZipFile(duplicate, "w") as archive:
            archive.writestr("same.bin", b"first")
            archive.writestr("same.bin", b"second")

    symlink = zipfile.ZipInfo("link.bin")
    symlink.create_system = 3
    symlink.external_attr = (stat.S_IFLNK | 0o777) << 16

    return {
        "duplicate": duplicate.getvalue(),
        "encrypted": _mark_zip_encrypted(_zip_with("secret.bin", b"ciphertext-placeholder")),
        "symlink": _zip_with(symlink, b"target.bin"),
        "traversal": _zip_with("../outside.bin", b"escape"),
        "compression_ratio": _zip_with(
            "expanded.bin",
            b"A" * (512 * 1024),
            compression=zipfile.ZIP_DEFLATED,
        ),
    }


def test_target_ticket_rejects_tamper_actor_host_and_expiry() -> None:
    target = _target()
    ticket = issue_target_ticket(
        target,
        "owner-a",
        ttl_sec=30,
        now=1_000,
        nonce="contract-nonce-0001",
        signing_key=_SIGNING_KEY,
    )

    verified = verify_target_ticket(
        ticket,
        actor_id="owner-a",
        exact_host="service.example",
        now=1_001,
        signing_key=_SIGNING_KEY,
    )
    assert verified.target.addresses == ("127.0.0.1",)

    body, signature = ticket.split(".", 1)
    flipped = ("A" if signature[0] != "A" else "B") + signature[1:]
    cases = (
        (f"{body}.{flipped}", "owner-a", "service.example", 1_001),
        (ticket, "owner-b", "service.example", 1_001),
        (ticket, "owner-a", "other.example", 1_001),
        (ticket, "owner-a", "service.example", 1_030),
    )
    for candidate, actor_id, host, now in cases:
        with pytest.raises(TargetTicketError):
            verify_target_ticket(
                candidate,
                actor_id=actor_id,
                exact_host=host,
                now=now,
                signing_key=_SIGNING_KEY,
            )


def test_port_cap_accepts_64_and_rejects_65_before_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    scans: list[list[int]] = []

    def fake_scan(
        _target: PinnedTarget,
        ports: list[int],
        *,
        deadline: float | None,
    ) -> list[dict[str, Any]]:
        del deadline
        scans.append(list(ports))
        return [{"port": port, "state": "closed", "probes": ["tcp_connect"]} for port in ports]

    monkeypatch.setattr(hosts, "_scan_ports", fake_scan)
    monkeypatch.setattr(
        local_binaries,
        "nmap_connect_scan",
        lambda *_args, **_kwargs: {"ok": False, "error": "disabled_in_contract_test"},
    )
    monkeypatch.setattr(
        local_binaries,
        "dig_records",
        lambda *_args, **_kwargs: {"ok": False, "error": "disabled_in_contract_test"},
    )
    monkeypatch.setattr(local_binaries, "inventory", lambda: {})

    accepted = list(range(1, 65))
    report = hosts.audit_target(_target(), accepted, rehearsal=False)
    assert report["ok"] is True
    assert scans == [accepted]

    with pytest.raises(ValueError, match="at most 64 ports"):
        hosts.audit_target(_target(), list(range(1, 66)), rehearsal=False)
    assert scans == [accepted], "the 65th port reached the probe boundary"


@pytest.mark.parametrize(("variant", "archive"), _unsafe_zip_variants().items())
def test_zip_metadata_variants_fail_closed(variant: str, archive: bytes) -> None:
    report = artifacts.analyze_bytes(archive, f"{variant}.zip")

    assert report["ok"] is True
    assert report["kind"] == "zip"
    assert report["format"]["readable"] is False
    assert report["format"]["reason"] == "unsafe_archive"
    assert report["format"]["findings"] == [{"code": "unsafe_archive", "detail": "ValueError"}]


def test_zip_rewrite_cannot_cross_output_cap() -> None:
    source = _zip_with("payload.bin", b"A")
    cap = len(source) + 32

    with pytest.raises(ValueError, match="output cap"):
        artifacts.apply_patches(
            source,
            [{"kind": "zip_replace", "name": "payload.bin", "bytes": (b"B" * 256).hex()}],
            max_output_bytes=cap,
        )


def test_credentials_are_redacted_from_artifact_and_protocol_evidence() -> None:
    password = "engineer-password-4815"
    bearer = "bearer-contract-token-0123456789"
    query_token = "query-contract-secret-9182"
    cookie = "cookie-contract-secret-7744"
    material = (
        f"password={password}\n"
        f"Authorization: Bearer {bearer}\n"
        f"https://operator:{password}@service.example/path?token={query_token}\n"
    ).encode()

    artifact_report = artifacts.analyze_bytes(material, "credentials.bin")
    projections = {
        "artifact": json.dumps(artifact_report, sort_keys=True),
        "text": redact_text(material.decode()),
        "url": redact_url(f"https://operator:{password}@service.example/path?token={query_token}"),
        "cookie": redact_header("set-cookie", f"session={cookie}; HttpOnly"),
        "challenge": redact_header("www-authenticate", f"Bearer token={bearer}"),
    }
    rendered = "\n".join(projections.values())

    for secret in (password, bearer, query_token, cookie):
        assert secret not in rendered
    assert "[REDACTED" in rendered
    assert "service.example/path" in projections["url"]
    assert "operator@" not in projections["url"]


def test_embedded_urls_project_all_query_values_userinfo_and_fragments() -> None:
    password = "userinfo-contract-password-4815"
    ordinary_query_value = "opaque-debug-value-7744"
    fragment = "fragment-contract-secret-9182"
    url = (
        f"https://operator:{password}@service.example:8443/path"
        f"?debug={ordinary_query_value}&empty=#{fragment}"
    )

    projections = (redact_text(f"prefix {url} suffix", limit=1024), redact_url(url, limit=1024))

    for rendered in projections:
        assert password not in rendered
        assert ordinary_query_value not in rendered
        assert fragment not in rendered
        assert "operator@" not in rendered
        assert "service.example:8443/path" in rendered
        assert "debug=%5BREDACTED%5D" in rendered
        assert "empty=" in rendered


def test_malformed_embedded_url_fails_closed_without_query_value() -> None:
    query_value = "malformed-url-query-secret-1911"

    rendered = redact_text(
        f"prefix https://service.example:not-a-port/path?debug={query_value} suffix",
        limit=1024,
    )

    assert query_value not in rendered
    assert "https://[REDACTED_INVALID_URL]" in rendered


def test_dns_pin_connect_uses_ip_but_preserves_logical_host_and_sni(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolved: list[str] = []
    connected: list[tuple[tuple[str, int], float]] = []
    sent: list[bytes] = []
    sni: list[str | None] = []

    def fake_resolve(host: str, *, deadline: float | None) -> list[Any]:
        del deadline
        resolved.append(host)
        return [(None, None, None, None, ("127.0.0.1", 0))]

    class FakeSocket:
        def __enter__(self) -> FakeSocket:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def close(self) -> None:
            return None

        def settimeout(self, _timeout: float) -> None:
            return None

        def sendall(self, payload: bytes) -> None:
            sent.append(payload)

        def recv(self, _size: int) -> bytes:
            return b"HTTP/1.0 200 OK\r\nServer: synthetic\r\n\r\n"

    class FakeTLSContext:
        check_hostname = True
        verify_mode = 0

        def wrap_socket(self, raw: FakeSocket, *, server_hostname: str | None) -> FakeSocket:
            sni.append(server_hostname)
            return raw

    def fake_connect(address: tuple[str, int], *, timeout: float) -> FakeSocket:
        connected.append((address, timeout))
        return FakeSocket()

    monkeypatch.setattr(hosts, "_bounded_getaddrinfo", fake_resolve)
    monkeypatch.setattr(hosts.socket, "create_connection", fake_connect)
    monkeypatch.setattr(hosts.ssl, "create_default_context", FakeTLSContext)

    target = hosts.authorize_target("service.example", source_token="service.example")
    response = hosts._http_exchange(  # noqa: SLF001 - contract covers the connect boundary
        target,
        443,
        "/",
        use_tls=True,
        deadline=None,
    )

    assert response["status"] == "HTTP/1.0 200 OK"
    assert resolved == ["service.example"]
    assert connected[0][0] == ("127.0.0.1", 443)
    assert sni == ["service.example"]
    assert b"Host: service.example\r\n" in sent[0]
