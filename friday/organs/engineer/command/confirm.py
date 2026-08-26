"""Separately authenticated owner-confirmation proofs.

The command-source secret cannot mint these. The minting API takes an exact
confirmation row/update identity, command digest, expiry and a one-shot nonce.
It does not accept an authority boolean or an unverified hash. Integration
must only call ``seal`` after authenticating that confirmation message.
"""

from __future__ import annotations

import hmac
import os
import secrets
import time
from hashlib import sha256
from typing import Any

from .contracts import (
    ALLOWED_CHANNELS,
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


def _mac(secret: bytes, payload: dict[str, Any]) -> str:
    return hmac.new(secret, canonical_json_bytes(payload), sha256).hexdigest()


class OwnerConfirmationAuthority:
    """Seals a distinct current-owner confirmation source. Separate secret from OwnerSource."""

    def __init__(self, secret: bytes, *, clock: Any = None) -> None:
        if not isinstance(secret, (bytes, bytearray)) or len(secret) < 32:
            raise CommandError("invalid_confirm_secret")
        self._secret = bytes(secret)
        self._clock = clock or (lambda: int(time.time()))

    @classmethod
    def from_env(cls, name: str = "FRIDAY_ENGINEER_OWNER_CONFIRM_SECRET") -> OwnerConfirmationAuthority:
        raw = os.environ.get(name, "")
        if not raw:
            raise CommandError("confirm_secret_missing")
        secret = raw.encode("utf-8") if isinstance(raw, str) else bytes(raw)
        return cls(secret)

    def seal(
        self,
        *,
        actor_id: str,
        tenant_id: str,
        conversation_id: str,
        channel: str,
        confirmation_row_id: str,
        confirmation_update_id: str,
        command_digest: str,
        expires_at: int,
        nonce: str | None = None,
    ) -> OwnerConfirmation:
        channel_text = _as_token(channel, code="invalid_channel", limit=32)
        if channel_text not in ALLOWED_CHANNELS:
            raise CommandError("invalid_channel")
        digest = _as_token(command_digest, code="invalid_command_digest", limit=64)
        if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
            raise CommandError("invalid_command_digest")
        if isinstance(expires_at, bool) or not isinstance(expires_at, int):
            raise CommandError("invalid_confirmation_expiry")
        now = int(self._clock())
        if expires_at <= now or expires_at > now + 3600:
            raise CommandError("invalid_confirmation_expiry")
        nonce_text = nonce if nonce is not None else secrets.token_hex(16)
        nonce_text = _as_token(nonce_text, code="invalid_confirmation", limit=64)
        confirmation = OwnerConfirmation(
            actor_id=_as_token(actor_id, code="invalid_actor", limit=128),
            tenant_id=_as_token(tenant_id, code="invalid_tenant", limit=128),
            conversation_id=_as_token(conversation_id, code="invalid_conversation", limit=128),
            channel=channel_text,
            confirmation_row_id=_as_token(confirmation_row_id, code="invalid_confirmation_row", limit=128),
            confirmation_update_id=_as_token(confirmation_update_id, code="invalid_confirmation_update", limit=128),
            command_digest=digest,
            expires_at=int(expires_at),
            nonce=nonce_text,
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
        if (
            confirmation.confirmation_row_id == source.source_row_id
            and confirmation.confirmation_update_id == source.telegram_update_id
        ):
            raise CommandError("confirmation_not_distinct")
        if int(confirmation.expires_at) <= int(self._clock()):
            raise CommandError("confirmation_expired")
        return confirmation
