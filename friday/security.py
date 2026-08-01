"""Shared authentication primitives for trusted local bridges."""

from __future__ import annotations

import hashlib
import hmac
import time
from dataclasses import dataclass

_NONCE_HEX = frozenset("0123456789abcdef")


@dataclass(frozen=True)
class SignedBridgeIdentity:
    external_user_id: str
    chat_id: str
    timestamp: int
    nonce: str = ""


def _valid_nonce(nonce: str) -> bool:
    # A single-use nonce is a 32-char lowercase hex string (uuid4().hex).
    return isinstance(nonce, str) and len(nonce) == 32 and all(char in _NONCE_HEX for char in nonce)


def bridge_canonical_message(
    timestamp: int | str,
    method: str,
    path: str,
    external_user_id: str,
    chat_id: str,
    nonce: str,
    body: bytes,
) -> bytes:
    digest = hashlib.sha256(body).hexdigest()
    value = "\n".join(
        (
            str(timestamp),
            method.upper(),
            path,
            str(external_user_id),
            str(chat_id),
            str(nonce),
            digest,
        )
    )
    return value.encode("utf-8")


def sign_bridge_request(
    secret: str,
    *,
    timestamp: int | str,
    method: str,
    path: str,
    external_user_id: str,
    chat_id: str,
    nonce: str,
    body: bytes,
) -> str:
    if not secret:
        raise ValueError("Bridge secret is not configured")
    canonical = bridge_canonical_message(
        timestamp,
        method,
        path,
        external_user_id,
        chat_id,
        nonce,
        body,
    )
    return hmac.new(secret.encode("utf-8"), canonical, hashlib.sha256).hexdigest()


def verify_bridge_request(
    secret: str,
    *,
    timestamp: str,
    method: str,
    path: str,
    external_user_id: str,
    chat_id: str,
    nonce: str,
    body: bytes,
    signature: str,
    max_age_sec: int,
    now: int | None = None,
) -> SignedBridgeIdentity:
    if not secret:
        raise ValueError("Bridge authentication is disabled")
    try:
        parsed_timestamp = int(timestamp)
    except (TypeError, ValueError) as exc:
        raise ValueError("Invalid bridge timestamp") from exc
    current = int(time.time()) if now is None else int(now)
    if abs(current - parsed_timestamp) > max(10, int(max_age_sec)):
        raise ValueError("Bridge request timestamp is outside the allowed window")
    if not external_user_id.isdigit():
        raise ValueError("Invalid external user id")
    if not chat_id or len(chat_id) > 32 or not chat_id.lstrip("-").isdigit():
        raise ValueError("Invalid chat id")
    if not _valid_nonce(nonce):
        raise ValueError("Invalid bridge nonce")
    expected = sign_bridge_request(
        secret,
        timestamp=parsed_timestamp,
        method=method,
        path=path,
        external_user_id=external_user_id,
        chat_id=chat_id,
        nonce=nonce,
        body=body,
    )
    # Bytes, not str. `hmac.compare_digest` raises TypeError when either str argument
    # holds a character above U+00FF — and a header value may: both HTTP parsers uvicorn
    # can use accept obs-text bytes (0x80-0xFF), which decode to exactly that range. The
    # TypeError escaped the middleware's handlers, so an unauthenticated caller got a 500
    # with a traceback instead of a 401, the attempt was never written to the audit log,
    # and it never counted against the failed-authentication budget that produces a 429.
    if not signature or not hmac.compare_digest(
        expected.encode("ascii"), signature.casefold().encode("utf-8")
    ):
        raise ValueError("Invalid bridge signature")
    return SignedBridgeIdentity(external_user_id, chat_id, parsed_timestamp, nonce)
