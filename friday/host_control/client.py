"""Authenticated, bounded Unix-socket client for the native host agent.

The client deliberately owns no retry loop.  A caller may retry a request only
after receiving :class:`HostControlUnavailable` or
:class:`HostControlRejected`.  Once an effectful request might have reached the
agent, loss of the signed response is reported as
:class:`HostControlOutcomeUnknown` and must be reconciled by job/idempotency
identity instead of being sent again blindly.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import os
import secrets
import socket
import stat
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from .contracts import (
    MAX_WIRE_BYTES,
    PROTOCOL_VERSION,
    ContractError,
    RequestEnvelope,
    WireRequest,
    body_sha256,
    canonical_json_bytes,
    decode_canonical_json,
)

_REQUEST_DOMAIN = b"friday-host-agent-request-v1"
_RESPONSE_DOMAIN = b"friday-host-agent-response-v1"
_RECEIPT_DOMAIN = b"friday-host-agent-receipt-v1"
_EMPTY_PLAN_DIGEST = "0" * 64
_RESPONSE_FIELDS = {
    "agent_id",
    "ok",
    "protocol_version",
    "request_id",
    "result",
    "signature",
}
_SAFE_ERROR_CODE = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.:-")


class HostControlClientError(RuntimeError):
    """Base class for content-free host-control failures."""

    def __init__(self, code: str, message: str, *, request_id: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.request_id = request_id


class HostControlRequestError(HostControlClientError):
    """The locally supplied request could not satisfy the wire contract."""


class HostControlUnavailable(HostControlClientError):
    """The request did not cross a possible host-side effect boundary."""

    effect_boundary_crossed = False


class HostControlRejected(HostControlClientError):
    """The authenticated host agent explicitly rejected the request."""

    effect_boundary_crossed = False


class HostControlProtocolFailure(HostControlClientError):
    """A non-effectful response failed framing, identity, or authentication."""

    effect_boundary_crossed = False


class HostControlOutcomeUnknown(HostControlClientError):
    """An effectful request may have run, but no trustworthy receipt arrived."""

    effect_boundary_crossed = True


@dataclass(frozen=True, slots=True)
class _PreparedRequest:
    request_id: str
    frame: bytes


def load_host_agent_key(path: str | Path) -> bytes:
    """Read one private 32-64 byte regular key without following symlinks.

    The read is explicitly capped, the opened inode is compared with ``lstat``,
    and a second ``fstat`` closes the replacement/truncation race around the
    read.  Parent symlinks are rejected as well so an operator can reason about
    exactly which secret path is admitted.
    """

    key_path = Path(path)
    if not key_path.is_absolute() or os.path.normpath(str(key_path)) != str(key_path):
        raise HostControlRequestError("invalid_key_path", "host-agent key path is invalid")
    try:
        if key_path.resolve(strict=True) != key_path:
            raise HostControlRequestError("invalid_key_path", "host-agent key path is invalid")
        before = key_path.lstat()
    except HostControlRequestError:
        raise
    except (OSError, RuntimeError) as exc:
        raise HostControlRequestError("key_unavailable", "host-agent key is unavailable") from exc
    if stat.S_ISLNK(before.st_mode):
        raise HostControlRequestError("invalid_key_path", "host-agent key path is invalid")

    descriptor = -1
    try:
        descriptor = os.open(
            key_path,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
            or opened.st_nlink != 1
            or opened.st_uid not in {0, os.geteuid()}
            or opened.st_mode & (stat.S_IRWXG | stat.S_IRWXO)
            or not 32 <= opened.st_size <= 64
        ):
            raise HostControlRequestError("insecure_key", "host-agent key is not private")
        chunks: list[bytes] = []
        remaining = 65
        while remaining:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        key = b"".join(chunks)
        after = os.fstat(descriptor)
        if (
            not 32 <= len(key) <= 64
            or len(key) != opened.st_size
            or (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
            != (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)
        ):
            raise HostControlRequestError("invalid_key", "host-agent key changed while reading")
        return key
    except HostControlRequestError:
        raise
    except OSError as exc:
        raise HostControlRequestError("key_unavailable", "host-agent key is unavailable") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


class HostControlClient:
    """One-request-per-connection client for the authenticated host agent."""

    def __init__(
        self,
        socket_path: str | Path,
        *,
        key_file: str | Path,
        agent_id: str,
        timeout_sec: float = 5.0,
        request_ttl_sec: int = 30,
        clock: Callable[[], float] = time.time,
    ) -> None:
        path = Path(socket_path)
        if not path.is_absolute() or os.path.normpath(str(path)) != str(path):
            raise HostControlRequestError("invalid_socket_path", "host-agent socket path is invalid")
        if not agent_id or len(agent_id) > 128:
            raise HostControlRequestError("invalid_agent_id", "host-agent identity is invalid")
        if isinstance(timeout_sec, bool) or not 0.05 <= float(timeout_sec) <= 3_600.0:
            raise HostControlRequestError("invalid_timeout", "host-agent timeout is invalid")
        if isinstance(request_ttl_sec, bool) or not 1 <= request_ttl_sec <= 300:
            raise HostControlRequestError("invalid_ttl", "host-agent request lifetime is invalid")
        self.socket_path = path
        self.agent_id = agent_id
        self.timeout_sec = float(timeout_sec)
        self.request_ttl_sec = request_ttl_sec
        self._key = load_host_agent_key(key_file)
        self._clock = clock
        self._sequence_lock = threading.Lock()
        # A random high-entropy epoch prevents durable replay-ledger collisions
        # across backend restarts; increments make concurrency within one process
        # deterministic and collision-free.
        self._sequence = secrets.randbelow(2**62 - 2**20) + 1

    async def call(
        self,
        method: str,
        body: dict[str, Any],
        *,
        job_id: str,
        actor_id: str,
        own_id: str,
        idempotency_key: str,
        plan_digest: str,
        approval_receipt_id: str | None = None,
        effectful: bool = False,
        timeout_sec: float | None = None,
    ) -> dict[str, Any]:
        """Send one request and return only an authenticated result object."""

        prepared = self._prepare_request(
            method,
            body,
            job_id=job_id,
            actor_id=actor_id,
            own_id=own_id,
            idempotency_key=idempotency_key,
            plan_digest=plan_digest,
            approval_receipt_id=approval_receipt_id,
        )
        timeout = self._validated_timeout(timeout_sec)
        writer: asyncio.StreamWriter | None = None
        possible_effect = False
        try:
            self._assert_socket()
            async with asyncio.timeout(timeout):
                reader, writer = await asyncio.open_unix_connection(
                    str(self.socket_path), limit=MAX_WIRE_BYTES + 1
                )
                # Once write() accepts an effectful frame, the peer may receive
                # it even when drain/read subsequently fails.
                possible_effect = effectful
                writer.write(prepared.frame)
                await writer.drain()
                raw = await self._read_async_frame(reader)
            return self._verify_response(raw, request_id=prepared.request_id)
        except HostControlRejected:
            raise
        except HostControlProtocolFailure as exc:
            if possible_effect:
                raise HostControlOutcomeUnknown(
                    "response_untrusted",
                    "host action outcome is unknown; reconcile the job before retrying",
                    request_id=prepared.request_id,
                ) from exc
            raise
        except asyncio.CancelledError as exc:
            if possible_effect:
                raise HostControlOutcomeUnknown(
                    "request_cancelled_after_send",
                    "host action outcome is unknown; reconcile the job before retrying",
                    request_id=prepared.request_id,
                ) from exc
            raise
        except (TimeoutError, OSError, asyncio.IncompleteReadError, ValueError) as exc:
            if possible_effect:
                raise HostControlOutcomeUnknown(
                    "transport_lost_after_send",
                    "host action outcome is unknown; reconcile the job before retrying",
                    request_id=prepared.request_id,
                ) from exc
            raise HostControlUnavailable(
                "agent_unavailable",
                "host agent is unavailable",
                request_id=prepared.request_id,
            ) from exc
        finally:
            if writer is not None:
                writer.close()
                with contextlib.suppress(OSError, TimeoutError):
                    await asyncio.wait_for(writer.wait_closed(), timeout=min(timeout, 1.0))

    async def handshake(self, *, timeout_sec: float | None = None) -> dict[str, Any]:
        metadata = self._probe_metadata("handshake")
        result = await self.call(
            "Handshake",
            {"client_protocol_version": PROTOCOL_VERSION},
            **metadata,
            effectful=False,
            timeout_sec=timeout_sec,
        )
        if result.get("accepted") is not True or result.get("agent_id") != self.agent_id:
            raise HostControlProtocolFailure("invalid_handshake", "host-agent handshake is invalid")
        versions = result.get("protocol_versions")
        if not isinstance(versions, list) or PROTOCOL_VERSION not in versions:
            raise HostControlProtocolFailure("unsupported_protocol", "host-agent protocol is not compatible")
        return result

    async def health(self, *, timeout_sec: float | None = None) -> dict[str, Any]:
        return await self.call(
            "Health",
            {},
            **self._probe_metadata("health"),
            effectful=False,
            timeout_sec=timeout_sec,
        )

    def handshake_sync(self, *, timeout_sec: float | None = None) -> dict[str, Any]:
        """Perform the same authenticated handshake without an event loop."""

        timeout = self._validated_timeout(timeout_sec)
        prepared = self._prepare_request(
            "Handshake",
            {"client_protocol_version": PROTOCOL_VERSION},
            **self._probe_metadata("handshake"),
        )
        connection: socket.socket | None = None
        deadline = time.monotonic() + timeout
        try:
            self._assert_socket()
            connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            connection.settimeout(self._remaining(deadline))
            connection.connect(str(self.socket_path))
            connection.settimeout(self._remaining(deadline))
            connection.sendall(prepared.frame)
            connection.shutdown(socket.SHUT_WR)
            raw = self._read_sync_frame(connection, deadline=deadline)
            result = self._verify_response(raw, request_id=prepared.request_id)
        except HostControlRejected:
            raise
        except HostControlProtocolFailure:
            raise
        except (TimeoutError, OSError, ValueError) as exc:
            raise HostControlUnavailable(
                "agent_unavailable",
                "host agent is unavailable",
                request_id=prepared.request_id,
            ) from exc
        finally:
            if connection is not None:
                connection.close()
        if result.get("accepted") is not True or result.get("agent_id") != self.agent_id:
            raise HostControlProtocolFailure("invalid_handshake", "host-agent handshake is invalid")
        versions = result.get("protocol_versions")
        if not isinstance(versions, list) or PROTOCOL_VERSION not in versions:
            raise HostControlProtocolFailure("unsupported_protocol", "host-agent protocol is not compatible")
        return result

    def available(self, *, timeout_sec: float | None = None) -> bool:
        """Return a bounded, fail-closed startup availability probe."""

        try:
            self.handshake_sync(timeout_sec=timeout_sec)
        except HostControlClientError:
            return False
        return True

    def verify_signed_bytes(self, payload: bytes, signature: str, *, domain: bytes) -> bool:
        """Verify an agent artifact without exposing the shared key to callers."""

        if (
            domain not in {_RESPONSE_DOMAIN, _RECEIPT_DOMAIN}
            or not isinstance(payload, bytes)
            or len(payload) > MAX_WIRE_BYTES
            or not isinstance(signature, str)
            or len(signature) != 64
            or any(character not in "0123456789abcdef" for character in signature)
        ):
            return False
        return hmac.compare_digest(signature, self._sign(payload, domain=domain))

    def verify_receipt_signature(self, agent_id: str, payload: bytes, signature: str) -> bool:
        """Adapter matching ``host_control.receipts.SignatureVerifier``."""

        return agent_id == self.agent_id and self.verify_signed_bytes(
            payload,
            signature,
            domain=_RECEIPT_DOMAIN,
        )

    def _prepare_request(
        self,
        method: str,
        body: dict[str, Any],
        *,
        job_id: str,
        actor_id: str,
        own_id: str,
        idempotency_key: str,
        plan_digest: str,
        approval_receipt_id: str | None = None,
    ) -> _PreparedRequest:
        request_id = f"request:{secrets.token_hex(24)}"
        now = int(self._clock())
        try:
            unsigned = RequestEnvelope(
                protocol_version=PROTOCOL_VERSION,
                request_id=request_id,
                agent_id=self.agent_id,
                sequence=self._next_sequence(),
                issued_at=now,
                expires_at=now + self.request_ttl_sec,
                method=method,
                job_id=job_id,
                actor_id=actor_id,
                own_id=own_id,
                idempotency_key=idempotency_key,
                plan_digest=plan_digest,
                approval_receipt_id=approval_receipt_id,
                body_sha256=body_sha256(body),
                signature="",
            )
            signature = self._sign(unsigned.signing_bytes(), domain=_REQUEST_DOMAIN)
            request = WireRequest.create(replace(unsigned, signature=signature), body)
            encoded = request.encode()
        except (ContractError, TypeError, ValueError) as exc:
            raise HostControlRequestError(
                "invalid_request", "host-control request does not satisfy the wire contract"
            ) from exc
        if len(encoded) + 1 > MAX_WIRE_BYTES:
            raise HostControlRequestError("request_too_large", "host-control request is too large")
        return _PreparedRequest(request_id=request_id, frame=encoded + b"\n")

    def _probe_metadata(self, probe: str) -> dict[str, Any]:
        nonce = secrets.token_hex(16)
        return {
            "job_id": f"probe:{probe}:{nonce}",
            "actor_id": "backend:startup",
            "own_id": "backend:startup",
            "idempotency_key": f"probe:{nonce}",
            "plan_digest": _EMPTY_PLAN_DIGEST,
        }

    def _next_sequence(self) -> int:
        with self._sequence_lock:
            value = self._sequence
            self._sequence += 1
            if self._sequence >= 2**63 - 1:
                self._sequence = secrets.randbelow(2**62 - 2**20) + 1
            return value

    def _validated_timeout(self, value: float | None) -> float:
        timeout = self.timeout_sec if value is None else value
        if isinstance(timeout, bool) or not 0.05 <= float(timeout) <= 3_600.0:
            raise HostControlRequestError("invalid_timeout", "host-agent timeout is invalid")
        return float(timeout)

    @staticmethod
    def _remaining(deadline: float) -> float:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("host-agent deadline elapsed")
        return remaining

    def _assert_socket(self) -> None:
        try:
            observed = self.socket_path.lstat()
        except OSError as exc:
            raise HostControlUnavailable("agent_unavailable", "host agent is unavailable") from exc
        if stat.S_ISLNK(observed.st_mode) or not stat.S_ISSOCK(observed.st_mode):
            raise HostControlUnavailable("invalid_agent_socket", "host agent is unavailable")

    async def _read_async_frame(self, reader: asyncio.StreamReader) -> bytes:
        try:
            framed = await reader.readuntil(b"\n")
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError) as exc:
            raise HostControlProtocolFailure(
                "invalid_framing", "host-agent response framing is invalid"
            ) from exc
        if not framed or len(framed) > MAX_WIRE_BYTES or not framed.endswith(b"\n"):
            raise HostControlProtocolFailure("invalid_framing", "host-agent response framing is invalid")
        trailing = await reader.read(MAX_WIRE_BYTES + 1)
        if trailing:
            raise HostControlProtocolFailure("multiple_frames", "host-agent response framing is invalid")
        return framed[:-1]

    def _read_sync_frame(self, connection: socket.socket, *, deadline: float) -> bytes:
        framed = bytearray()
        while True:
            connection.settimeout(self._remaining(deadline))
            chunk = connection.recv(min(64 * 1024, MAX_WIRE_BYTES + 1 - len(framed)))
            if not chunk:
                break
            framed.extend(chunk)
            if len(framed) > MAX_WIRE_BYTES:
                raise HostControlProtocolFailure(
                    "response_too_large", "host-agent response exceeds the byte limit"
                )
        if not framed or not framed.endswith(b"\n") or framed.count(b"\n") != 1:
            raise HostControlProtocolFailure("invalid_framing", "host-agent response framing is invalid")
        return bytes(framed[:-1])

    def _verify_response(self, raw: bytes, *, request_id: str) -> dict[str, Any]:
        try:
            value = decode_canonical_json(raw, maximum=MAX_WIRE_BYTES)
        except (ContractError, RecursionError) as exc:
            raise HostControlProtocolFailure(
                "invalid_response_json", "host-agent response is invalid"
            ) from exc
        if not isinstance(value, dict) or set(value) != _RESPONSE_FIELDS:
            raise HostControlProtocolFailure("invalid_response_fields", "host-agent response is invalid")
        signature = value.get("signature")
        if (
            not isinstance(signature, str)
            or len(signature) != 64
            or any(character not in "0123456789abcdef" for character in signature)
        ):
            raise HostControlProtocolFailure("invalid_response_signature", "host-agent response is invalid")
        unsigned = dict(value)
        del unsigned["signature"]
        expected = self._sign(canonical_json_bytes(unsigned, maximum=MAX_WIRE_BYTES), domain=_RESPONSE_DOMAIN)
        if not hmac.compare_digest(signature, expected):
            raise HostControlProtocolFailure(
                "invalid_response_signature", "host-agent response authentication failed"
            )
        if unsigned["protocol_version"] != PROTOCOL_VERSION:
            raise HostControlProtocolFailure(
                "unsupported_protocol", "host-agent response protocol is incompatible"
            )
        if unsigned["agent_id"] != self.agent_id:
            raise HostControlProtocolFailure(
                "agent_identity_mismatch", "host-agent response identity is invalid"
            )
        if unsigned["request_id"] != request_id:
            raise HostControlProtocolFailure(
                "request_identity_mismatch", "host-agent response request identity is invalid"
            )
        if not isinstance(unsigned["ok"], bool) or not isinstance(unsigned["result"], dict):
            raise HostControlProtocolFailure("invalid_response_types", "host-agent response is invalid")
        result = unsigned["result"]
        if not unsigned["ok"]:
            code = result.get("error_code")
            if (
                not isinstance(code, str)
                or not 1 <= len(code) <= 64
                or any(character not in _SAFE_ERROR_CODE for character in code)
            ):
                raise HostControlProtocolFailure("invalid_rejection", "host-agent rejection is invalid")
            raise HostControlRejected(
                code,
                "host agent rejected the request",
                request_id=request_id,
            )
        return result

    def _sign(self, payload: bytes, *, domain: bytes) -> str:
        return hmac.new(self._key, domain + b"\x00" + payload, hashlib.sha256).hexdigest()


__all__ = [
    "HostControlClient",
    "HostControlClientError",
    "HostControlOutcomeUnknown",
    "HostControlProtocolFailure",
    "HostControlRejected",
    "HostControlRequestError",
    "HostControlUnavailable",
    "load_host_agent_key",
]
