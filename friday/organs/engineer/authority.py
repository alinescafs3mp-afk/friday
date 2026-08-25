"""Short-lived, actor-bound authority for engineer network tools.

The ticket contains no credential.  A process-local HMAC authenticates the
code-owned target selected from the current human turn, so model-authored tool
arguments cannot mint or widen network authority.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import secrets
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from .targets import PinnedTarget, is_forbidden_address, normalize_ip_address, parse_host_token

TICKET_VERSION = 1
DEFAULT_TTL_SEC = 90
MAX_TTL_SEC = 180
MAX_TICKET_CHARS = 4096
MAX_TICKET_ADDRESSES = 16
_PROCESS_SIGNING_KEY = secrets.token_bytes(32)


class TargetTicketError(ValueError):
    """A network authority ticket is absent, invalid, expired, or mismatched."""


@dataclass(frozen=True, slots=True)
class VerifiedTargetTicket:
    """Verified ticket projection; the signature and nonce are not exposed."""

    target: PinnedTarget
    actor_id: str
    expires_at: int


def _b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64decode(value: str) -> bytes:
    if not value or not value.isascii():
        raise TargetTicketError("target ticket encoding is invalid")
    padding = "=" * (-len(value) % 4)
    try:
        return base64.b64decode(value + padding, altchars=b"-_", validate=True)
    except (ValueError, TypeError) as exc:
        raise TargetTicketError("target ticket encoding is invalid") from exc


def _key(signing_key: bytes | None) -> bytes:
    key = _PROCESS_SIGNING_KEY if signing_key is None else bytes(signing_key)
    if len(key) < 32:
        raise ValueError("engineer ticket signing key must be at least 32 bytes")
    return key


def _canonical_payload(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("ascii")


def _canonical_addresses(addresses: Sequence[str]) -> tuple[str, ...]:
    if not addresses or len(addresses) > MAX_TICKET_ADDRESSES:
        raise TargetTicketError("target ticket address count is invalid")
    normalized: set[str] = set()
    for raw in addresses:
        try:
            address = normalize_ip_address(ipaddress.ip_address(str(raw)))
        except ValueError as exc:
            raise TargetTicketError("target ticket contains an invalid address") from exc
        if is_forbidden_address(address):
            raise TargetTicketError("target ticket contains a forbidden address")
        normalized.add(str(address))
    return tuple(
        sorted(
            normalized,
            key=lambda value: (
                ipaddress.ip_address(value).version,
                int(ipaddress.ip_address(value)),
            ),
        )
    )


def issue_target_ticket(
    target: PinnedTarget,
    actor_id: str,
    *,
    ttl_sec: int = DEFAULT_TTL_SEC,
    now: int | None = None,
    nonce: str | None = None,
    signing_key: bytes | None = None,
) -> str:
    """Sign an already-resolved target for one authenticated actor."""

    actor = str(actor_id or "").strip()
    if not actor or len(actor) > 256:
        raise ValueError("actor id is empty or too long")
    host, _port = parse_host_token(target.host)
    addresses = _canonical_addresses(target.addresses)
    ttl = int(ttl_sec)
    if not 1 <= ttl <= MAX_TTL_SEC:
        raise ValueError(f"target ticket ttl must be in 1..{MAX_TTL_SEC} seconds")
    issued_at = int(time.time() if now is None else now)
    nonce_value = str(nonce or secrets.token_urlsafe(18))
    if not 16 <= len(nonce_value) <= 96 or not nonce_value.isascii():
        raise ValueError("target ticket nonce is invalid")
    source_sha256 = str(target.source_sha256 or "")
    if len(source_sha256) != 64 or any(char not in "0123456789abcdef" for char in source_sha256):
        raise ValueError("target source fingerprint is invalid")
    payload: dict[str, Any] = {
        "a": list(addresses),
        "actor": actor,
        "exp": issued_at + ttl,
        "h": host,
        "iat": issued_at,
        "n": nonce_value,
        "p": target.implied_port,
        "src": source_sha256,
        "v": TICKET_VERSION,
    }
    body = _canonical_payload(payload)
    signature = hmac.new(_key(signing_key), body, hashlib.sha256).digest()
    return f"{_b64encode(body)}.{_b64encode(signature)}"


def verify_target_ticket(
    ticket: str,
    *,
    actor_id: str,
    exact_host: str,
    now: int | None = None,
    signing_key: bytes | None = None,
) -> VerifiedTargetTicket:
    """Verify HMAC, expiry, actor, exact normalized host, and pinned IPs."""

    encoded = str(ticket or "")
    if not encoded or len(encoded) > MAX_TICKET_CHARS or encoded.count(".") != 1:
        raise TargetTicketError("a valid target ticket is required")
    body_part, signature_part = encoded.split(".", 1)
    body = _b64decode(body_part)
    signature = _b64decode(signature_part)
    expected = hmac.new(_key(signing_key), body, hashlib.sha256).digest()
    if len(signature) != len(expected) or not hmac.compare_digest(signature, expected):
        raise TargetTicketError("target ticket signature is invalid")
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TargetTicketError("target ticket payload is invalid") from exc
    if not isinstance(payload, dict) or _canonical_payload(payload) != body:
        raise TargetTicketError("target ticket payload is not canonical")
    required = {"a", "actor", "exp", "h", "iat", "n", "p", "src", "v"}
    if set(payload) != required or payload.get("v") != TICKET_VERSION:
        raise TargetTicketError("target ticket version or fields are invalid")
    actor = str(actor_id or "").strip()
    ticket_actor = payload.get("actor")
    if not isinstance(ticket_actor, str) or not hmac.compare_digest(ticket_actor, actor):
        raise TargetTicketError("target ticket belongs to another actor")
    try:
        requested_host, _requested_port = parse_host_token(exact_host)
        ticket_host, _ticket_port = parse_host_token(str(payload.get("h") or ""))
    except ValueError as exc:
        raise TargetTicketError("target ticket host is invalid") from exc
    if not hmac.compare_digest(ticket_host, requested_host):
        raise TargetTicketError("target ticket does not authorize this exact host")
    nonce_value = payload.get("n")
    if not isinstance(nonce_value, str) or not 16 <= len(nonce_value) <= 96 or not nonce_value.isascii():
        raise TargetTicketError("target ticket nonce is invalid")
    issued_at = payload.get("iat")
    expires_at = payload.get("exp")
    current = int(time.time() if now is None else now)
    if (
        isinstance(issued_at, bool)
        or isinstance(expires_at, bool)
        or not isinstance(issued_at, int)
        or not isinstance(expires_at, int)
        or expires_at <= issued_at
        or expires_at - issued_at > MAX_TTL_SEC
        or issued_at > current + 5
        or current >= expires_at
    ):
        raise TargetTicketError("target ticket is expired or has invalid timing")
    raw_addresses = payload.get("a")
    if not isinstance(raw_addresses, list) or not all(isinstance(item, str) for item in raw_addresses):
        raise TargetTicketError("target ticket addresses are invalid")
    addresses = _canonical_addresses(raw_addresses)
    if list(addresses) != raw_addresses:
        raise TargetTicketError("target ticket addresses are not canonical")
    implied_port = payload.get("p")
    if implied_port is not None and (
        isinstance(implied_port, bool) or not isinstance(implied_port, int) or not 1 <= implied_port <= 65535
    ):
        raise TargetTicketError("target ticket port is invalid")
    source_sha256 = payload.get("src")
    if (
        not isinstance(source_sha256, str)
        or len(source_sha256) != 64
        or any(char not in "0123456789abcdef" for char in source_sha256)
    ):
        raise TargetTicketError("target ticket source fingerprint is invalid")
    target = PinnedTarget(
        host=ticket_host,
        addresses=addresses,
        implied_port=implied_port,
        source_token="",
        source_sha256=source_sha256,
    )
    return VerifiedTargetTicket(target=target, actor_id=actor, expires_at=expires_at)


__all__ = [
    "DEFAULT_TTL_SEC",
    "MAX_TTL_SEC",
    "TargetTicketError",
    "VerifiedTargetTicket",
    "issue_target_ticket",
    "verify_target_ticket",
]
