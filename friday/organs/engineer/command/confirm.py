"""Separately authenticated owner-confirmation proofs.

``ingest`` is the ingress seam: it records an already-authenticated owner
event and returns an opaque handle. ``seal`` consumes that handle. The
minting API does not accept raw row/update identifiers, an authority
boolean, or an unverified hash.
"""

from __future__ import annotations

import hmac
import json
import os
import secrets
import time
from hashlib import sha256
from typing import Any

from .contracts import (
    ALLOWED_CHANNELS,
    SCHEMA,
    CommandError,
    OwnerConfirmation,
    OwnerSource,
    canonical_json_bytes,
)


def _as_token(value: Any, *, code: str, limit: int) -> str:
    text = str(value or "").strip()
    if not text or len(text) > limit or "\x00" in text:
        raise CommandError(code)
    return text


def _hex_digest(value: Any, *, code: str) -> str:
    text = _as_token(value, code=code, limit=64)
    if len(text) != 64 or any(ch not in "0123456789abcdef" for ch in text):
        raise CommandError(code)
    return text


def _mac(secret: bytes, payload: dict[str, Any]) -> str:
    return hmac.new(secret, canonical_json_bytes(payload), sha256).hexdigest()


class OwnerConfirmationAuthority:
    """Seals a distinct current-owner confirmation from a stored ingress event."""

    def __init__(self, secret: bytes, *, clock: Any = None) -> None:
        if not isinstance(secret, (bytes, bytearray)) or len(secret) < 32:
            raise CommandError("invalid_confirm_secret")
        self._secret = bytes(secret)
        self._clock = clock or (lambda: int(time.time()))
        self._store: Any = None

    @classmethod
    def from_env(cls, name: str = "FRIDAY_ENGINEER_OWNER_CONFIRM_SECRET") -> OwnerConfirmationAuthority:
        raw = os.environ.get(name, "")
        if not raw:
            raise CommandError("confirm_secret_missing")
        secret = raw.encode("utf-8") if isinstance(raw, str) else bytes(raw)
        return cls(secret)

    def bind_store(self, store: Any) -> None:
        self._store = store

    def ingest(
        self,
        *,
        actor_id: str,
        tenant_id: str,
        conversation_id: str,
        channel: str,
        confirmation_row_id: str,
        confirmation_update_id: str,
        command_digest: str,
        body_hash: str,
        expires_at: int,
    ) -> str:
        """Record an ingress-authenticated confirmation event. Returns an opaque handle."""
        if self._store is None:
            raise CommandError("confirmation_ledger_unbound")
        channel_text = _as_token(channel, code="invalid_channel", limit=32)
        if channel_text not in ALLOWED_CHANNELS:
            raise CommandError("invalid_channel")
        if isinstance(expires_at, bool) or not isinstance(expires_at, int):
            raise CommandError("invalid_confirmation_expiry")
        now = int(self._clock())
        if expires_at <= now or expires_at > now + 3600:
            raise CommandError("invalid_confirmation_expiry")
        event = {
            "actor_id": _as_token(actor_id, code="invalid_actor", limit=128),
            "body_hash": _hex_digest(body_hash, code="invalid_confirmation_body"),
            "channel": channel_text,
            "command_digest": _hex_digest(command_digest, code="invalid_command_digest"),
            "confirmation_row_id": _as_token(confirmation_row_id, code="invalid_confirmation_row", limit=128),
            "confirmation_update_id": _as_token(
                confirmation_update_id, code="invalid_confirmation_update", limit=128
            ),
            "conversation_id": _as_token(conversation_id, code="invalid_conversation", limit=128),
            "expires_at": int(expires_at),
            "schema": SCHEMA,
            "tenant_id": _as_token(tenant_id, code="invalid_tenant", limit=128),
            "v": 4,
        }
        handle = secrets.token_hex(16)
        mac = _mac(self._secret, event)
        with self._store.transaction():
            self._store.insert_confirmation_event(
                handle=handle,
                payload_json=canonical_json_bytes(event).decode("ascii"),
                mac=mac,
                exp=int(expires_at),
            )
        return handle

    def seal(self, handle: str, *, command_digest: str) -> OwnerConfirmation:
        """Consume a stored ingress handle. Does not take raw row/update identifiers."""
        if self._store is None:
            raise CommandError("confirmation_ledger_unbound")
        handle_text = _as_token(handle, code="invalid_confirmation", limit=64)
        digest = _hex_digest(command_digest, code="invalid_command_digest")
        now = int(self._clock())
        with self._store.transaction():
            row = self._store.take_confirmation_event(handle_text, now=now)
            payload = json.loads(str(row["payload_json"]))
            if not isinstance(payload, dict):
                raise CommandError("invalid_confirmation")
            expected = _mac(self._secret, payload)
            if not hmac.compare_digest(expected, str(row["mac"] or "")):
                raise CommandError("invalid_destructive_approval")
            if str(payload.get("command_digest") or "") != digest:
                raise CommandError("destructive_digest_mismatch")
            if int(payload.get("expires_at") or 0) <= now:
                raise CommandError("confirmation_expired")
            nonce = secrets.token_hex(16)
            confirmation = OwnerConfirmation(
                actor_id=str(payload["actor_id"]),
                tenant_id=str(payload["tenant_id"]),
                conversation_id=str(payload["conversation_id"]),
                channel=str(payload["channel"]),
                confirmation_row_id=str(payload["confirmation_row_id"]),
                confirmation_update_id=str(payload["confirmation_update_id"]),
                command_digest=digest,
                expires_at=int(payload["expires_at"]),
                nonce=nonce,
                mac="",
            )
            mac = _mac(self._secret, confirmation.identity_payload())
            return OwnerConfirmation(
                actor_id=confirmation.actor_id,
                tenant_id=confirmation.tenant_id,
                conversation_id=confirmation.conversation_id,
                channel=confirmation.channel,
                confirmation_row_id=confirmation.confirmation_row_id,
                confirmation_update_id=confirmation.confirmation_update_id,
                command_digest=confirmation.command_digest,
                expires_at=confirmation.expires_at,
                nonce=confirmation.nonce,
                mac=mac,
            )

    def verify(self, confirmation: OwnerConfirmation, *, source: OwnerSource, command_digest: str) -> OwnerConfirmation:
        if not isinstance(confirmation, OwnerConfirmation):
            raise CommandError("destructive_confirmation_required")
        expected = _mac(self._secret, confirmation.identity_payload())
        if not hmac.compare_digest(expected, str(confirmation.mac or "")):
            raise CommandError("invalid_destructive_approval")
        if confirmation.channel not in ALLOWED_CHANNELS:
            raise CommandError("invalid_channel")
        if (
            confirmation.actor_id != source.actor_id
            or confirmation.tenant_id != source.tenant_id
            or confirmation.conversation_id != source.conversation_id
            or confirmation.channel != source.channel
        ):
            raise CommandError("destructive_source_mismatch")
        if confirmation.command_digest != command_digest:
            raise CommandError("destructive_digest_mismatch")
        if confirmation.confirmation_row_id == source.source_row_id:
            raise CommandError("confirmation_not_distinct")
        if confirmation.confirmation_update_id == source.telegram_update_id:
            raise CommandError("confirmation_not_distinct")
        if int(confirmation.expires_at) <= int(self._clock()):
            raise CommandError("confirmation_expired")
        return confirmation
