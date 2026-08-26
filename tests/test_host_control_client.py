from __future__ import annotations

import asyncio
import hashlib
import hmac
import os
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest

from friday.host_control.client import (
    HostControlClient,
    HostControlOutcomeUnknown,
    HostControlProtocolFailure,
    HostControlRejected,
    HostControlRequestError,
    HostControlUnavailable,
    load_host_agent_key,
)
from friday.host_control.contracts import (
    PROTOCOL_VERSION,
    WireRequest,
    canonical_json_bytes,
)
from friday_host_agent.authentication import HMACAuthenticator

_KEY = b"k" * 32
_AGENT_ID = "host-agent:test"
_DIGEST = "a" * 64
_RESPONSE_DOMAIN = b"friday-host-agent-response-v1"


def _key_file(tmp_path: Path) -> Path:
    path = tmp_path / "agent.key"
    path.write_bytes(_KEY)
    path.chmod(0o600)
    return path


def _client(tmp_path: Path, socket_path: Path, *, timeout: float = 1.0) -> HostControlClient:
    return HostControlClient(
        socket_path,
        key_file=_key_file(tmp_path),
        agent_id=_AGENT_ID,
        timeout_sec=timeout,
    )


def _signed_response(
    request_id: str,
    *,
    ok: bool = True,
    result: dict[str, Any] | None = None,
    agent_id: str = _AGENT_ID,
    protocol_version: str = PROTOCOL_VERSION,
) -> bytes:
    body = {
        "agent_id": agent_id,
        "ok": ok,
        "protocol_version": protocol_version,
        "request_id": request_id,
        "result": {} if result is None else result,
    }
    signature = hmac.new(
        _KEY,
        _RESPONSE_DOMAIN + b"\x00" + canonical_json_bytes(body),
        hashlib.sha256,
    ).hexdigest()
    return canonical_json_bytes({**body, "signature": signature})


Handler = Callable[[asyncio.StreamReader, asyncio.StreamWriter], Awaitable[None]]


async def _run_server(socket_path: Path, handler: Handler, operation: Awaitable[Any]) -> Any:
    server = await asyncio.start_unix_server(handler, path=str(socket_path))
    try:
        return await operation
    finally:
        server.close()
        await server.wait_closed()


async def _read_request(reader: asyncio.StreamReader) -> WireRequest:
    frame = await reader.readline()
    assert frame.endswith(b"\n")
    return WireRequest.decode(frame[:-1])


async def test_async_handshake_is_canonical_authenticated_and_fresh(tmp_path: Path) -> None:
    socket_path = tmp_path / "agent.sock"
    client = _client(tmp_path, socket_path)
    seen: list[WireRequest] = []
    authenticator = HMACAuthenticator(_KEY, agent_id=_AGENT_ID)

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        request = await _read_request(reader)
        authenticator.verify(request.envelope, request.body)
        seen.append(request)
        result = {
            "accepted": True,
            "agent_id": _AGENT_ID,
            "protocol_versions": [PROTOCOL_VERSION],
        }
        writer.write(_signed_response(request.envelope.request_id, result=result) + b"\n")
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    first = await _run_server(socket_path, handler, client.handshake())
    assert first["accepted"] is True

    # A second server instance sees a fresh request nonce and sequence.  The
    # sequence is never reused even though each call gets a separate socket.
    second = await _run_server(socket_path, handler, client.handshake())
    assert second["accepted"] is True
    assert seen[0].envelope.request_id != seen[1].envelope.request_id
    assert seen[0].envelope.sequence != seen[1].envelope.sequence
    assert seen[1].envelope.sequence == seen[0].envelope.sequence + 1
    assert seen[0].body == {"client_protocol_version": PROTOCOL_VERSION}


@pytest.mark.parametrize("failure", ["tamper", "stale", "wrong_agent"])
async def test_response_tamper_stale_nonce_and_wrong_agent_fail_closed(tmp_path: Path, failure: str) -> None:
    socket_path = tmp_path / "agent.sock"
    client = _client(tmp_path, socket_path)

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        request = await _read_request(reader)
        request_id = request.envelope.request_id
        agent_id = _AGENT_ID
        if failure == "stale":
            request_id = "request:stale"
        elif failure == "wrong_agent":
            agent_id = "host-agent:other"
        response = _signed_response(
            request_id,
            result={
                "accepted": True,
                "agent_id": _AGENT_ID,
                "protocol_versions": [PROTOCOL_VERSION],
            },
            agent_id=agent_id,
        )
        if failure == "tamper":
            # Keep canonical framing while invalidating the signature.
            response = response.replace(b'"accepted":true', b'"accepted":false')
        writer.write(response + b"\n")
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    with pytest.raises(HostControlProtocolFailure):
        await _run_server(socket_path, handler, client.handshake())


async def test_truncated_response_is_protocol_failure_for_read_only_call(tmp_path: Path) -> None:
    socket_path = tmp_path / "agent.sock"
    client = _client(tmp_path, socket_path)

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        request = await _read_request(reader)
        writer.write(_signed_response(request.envelope.request_id)[:25])
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    with pytest.raises(HostControlProtocolFailure, match="framing"):
        await _run_server(socket_path, handler, client.health())


async def test_disconnect_after_effectful_send_is_unknown_not_retryable(tmp_path: Path) -> None:
    socket_path = tmp_path / "agent.sock"
    client = _client(tmp_path, socket_path)
    received = asyncio.Event()

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await _read_request(reader)
        received.set()
        writer.close()
        await writer.wait_closed()

    operation = client.call(
        "RunAction",
        {"plan": {"opaque": "private"}},
        job_id="job:one",
        actor_id="actor:one",
        own_id="owner:one",
        idempotency_key="idempotency:one",
        plan_digest=_DIGEST,
        effectful=True,
    )
    with pytest.raises(HostControlOutcomeUnknown) as caught:
        await _run_server(socket_path, handler, operation)
    assert received.is_set()
    assert caught.value.effect_boundary_crossed is True
    assert "private" not in str(caught.value)


async def test_signed_rejection_is_known_and_safe_to_replan(tmp_path: Path) -> None:
    socket_path = tmp_path / "agent.sock"
    client = _client(tmp_path, socket_path)

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        request = await _read_request(reader)
        writer.write(
            _signed_response(
                request.envelope.request_id,
                ok=False,
                result={"error_code": "policy_denied"},
            )
            + b"\n"
        )
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    with pytest.raises(HostControlRejected) as caught:
        await _run_server(socket_path, handler, client.health())
    assert caught.value.code == "policy_denied"
    assert caught.value.effect_boundary_crossed is False


async def test_absent_socket_is_pre_effect_unavailable(tmp_path: Path) -> None:
    client = _client(tmp_path, tmp_path / "absent.sock")
    with pytest.raises(HostControlUnavailable) as caught:
        await client.call(
            "RunAction",
            {},
            job_id="job:one",
            actor_id="actor:one",
            own_id="owner:one",
            idempotency_key="idempotency:one",
            plan_digest=_DIGEST,
            effectful=True,
        )
    assert caught.value.effect_boundary_crossed is False


async def test_sync_handshake_and_availability_use_signed_exchange(tmp_path: Path) -> None:
    socket_path = tmp_path / "agent.sock"
    client = _client(tmp_path, socket_path)

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        request = await _read_request(reader)
        result = {
            "accepted": True,
            "agent_id": _AGENT_ID,
            "protocol_versions": [PROTOCOL_VERSION],
        }
        writer.write(_signed_response(request.envelope.request_id, result=result) + b"\n")
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_unix_server(handler, path=str(socket_path))
    try:
        result = await asyncio.to_thread(client.handshake_sync)
        assert result["accepted"] is True
    finally:
        server.close()
        await server.wait_closed()
    assert client.available(timeout_sec=0.05) is False


def test_key_loader_rejects_symlink_permissions_and_oversize(tmp_path: Path) -> None:
    key = _key_file(tmp_path)
    assert load_host_agent_key(key) == _KEY

    alias = tmp_path / "alias.key"
    alias.symlink_to(key)
    with pytest.raises(HostControlRequestError):
        load_host_agent_key(alias)

    key.chmod(0o640)
    with pytest.raises(HostControlRequestError, match="private"):
        load_host_agent_key(key)

    key.chmod(0o600)
    key.write_bytes(os.urandom(65))
    with pytest.raises(HostControlRequestError, match="private"):
        load_host_agent_key(key)


def test_receipt_verifier_keeps_key_inside_client(tmp_path: Path) -> None:
    client = _client(tmp_path, tmp_path / "agent.sock")
    payload = canonical_json_bytes({"receipt": "opaque"})
    signature = hmac.new(
        _KEY,
        b"friday-host-agent-receipt-v1\x00" + payload,
        hashlib.sha256,
    ).hexdigest()
    assert client.verify_receipt_signature(_AGENT_ID, payload, signature)
    assert not client.verify_receipt_signature("host-agent:other", payload, signature)
    assert not client.verify_receipt_signature(_AGENT_ID, payload + b"x", signature)
