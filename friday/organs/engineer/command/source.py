"""Authenticated owner-source proofs. Model/web/attachment cannot mint these."""

from __future__ import annotations

import hmac
import os
from hashlib import sha256
from typing import Any

from .contracts import (
    ALLOWED_CHANNELS,
    CommandError,
    DestructiveApproval,
    IsolationProfile,
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


class OwnerSourceAuthority:
    """Seals current authenticated source-row identity. Separate from command grants."""

    def __init__(self, secret: bytes) -> None:
        if not isinstance(secret, (bytes, bytearray)) or len(secret) < 32:
            raise CommandError("invalid_source_secret")
        self._secret = bytes(secret)

    @classmethod
    def from_env(cls, name: str = "FRIDAY_ENGINEER_OWNER_SOURCE_SECRET") -> OwnerSourceAuthority:
        raw = os.environ.get(name, "")
        if not raw:
            raise CommandError("source_secret_missing")
        secret = raw.encode("utf-8") if isinstance(raw, str) else bytes(raw)
        return cls(secret)

    def attest(
        self,
        *,
        actor_id: str,
        tenant_id: str,
        conversation_id: str,
        channel: str,
        source_row_id: str,
        source_hash: str,
        telegram_update_id: str,
        isolation_profile: IsolationProfile,
        idempotency_key: str,
        host_user_authorized: bool = False,
    ) -> OwnerSource:
        if not isinstance(isolation_profile, IsolationProfile):
            raise CommandError("invalid_isolation_profile")
        if isolation_profile is IsolationProfile.HOST_USER and not host_user_authorized:
            raise CommandError("host_user_authorization_required")
        if isolation_profile is IsolationProfile.ISOLATED_WORKSPACE and host_user_authorized:
            raise CommandError("invalid_isolation_profile")
        channel_text = _as_token(channel, code="invalid_channel", limit=32)
        if channel_text not in ALLOWED_CHANNELS:
            raise CommandError("invalid_channel")
        source_hash_text = _as_token(source_hash, code="invalid_source_hash", limit=64)
        if len(source_hash_text) != 64 or any(ch not in "0123456789abcdef" for ch in source_hash_text):
            raise CommandError("invalid_source_hash")
        source = OwnerSource(
            actor_id=_as_token(actor_id, code="invalid_actor", limit=128),
            tenant_id=_as_token(tenant_id, code="invalid_tenant", limit=128),
            conversation_id=_as_token(conversation_id, code="invalid_conversation", limit=128),
            channel=channel_text,
            source_row_id=_as_token(source_row_id, code="invalid_source_row", limit=128),
            source_hash=source_hash_text,
            telegram_update_id=_as_token(telegram_update_id, code="invalid_telegram_update", limit=128),
            isolation_profile=isolation_profile,
            host_user_authorized=bool(host_user_authorized),
            idempotency_key=_as_token(idempotency_key, code="invalid_request", limit=128),
            mac="",
        )
        mac = _mac(self._secret, source.identity_payload())
        return OwnerSource(
            actor_id=source.actor_id,
            tenant_id=source.tenant_id,
            conversation_id=source.conversation_id,
            channel=source.channel,
            source_row_id=source.source_row_id,
            source_hash=source.source_hash,
            telegram_update_id=source.telegram_update_id,
            isolation_profile=source.isolation_profile,
            host_user_authorized=source.host_user_authorized,
            idempotency_key=source.idempotency_key,
            mac=mac,
        )

    def verify(self, source: OwnerSource) -> OwnerSource:
        if not isinstance(source, OwnerSource):
            raise CommandError("invalid_owner_source")
        expected = _mac(self._secret, source.identity_payload())
        if not hmac.compare_digest(expected, str(source.mac or "")):
            raise CommandError("invalid_owner_source")
        if source.channel not in ALLOWED_CHANNELS:
            raise CommandError("invalid_channel")
        if source.isolation_profile is IsolationProfile.HOST_USER and not source.host_user_authorized:
            raise CommandError("host_user_authorization_required")
        if source.isolation_profile is IsolationProfile.ISOLATED_WORKSPACE and source.host_user_authorized:
            raise CommandError("invalid_isolation_profile")
        return source

    def approve_destructive(
        self,
        source: OwnerSource,
        *,
        confirmation_hash: str,
        command_digest: str,
    ) -> DestructiveApproval:
        self.verify(source)
        confirm = _as_token(confirmation_hash, code="invalid_confirmation", limit=64)
        digest = _as_token(command_digest, code="invalid_command_digest", limit=64)
        if len(confirm) != 64 or len(digest) != 64:
            raise CommandError("invalid_confirmation")
        approval = DestructiveApproval(
            source_hash=source.source_hash,
            confirmation_hash=confirm,
            command_digest=digest,
            mac="",
        )
        mac = _mac(self._secret, approval.identity_payload())
        return DestructiveApproval(
            source_hash=approval.source_hash,
            confirmation_hash=approval.confirmation_hash,
            command_digest=approval.command_digest,
            mac=mac,
        )

    def verify_destructive(self, source: OwnerSource, approval: DestructiveApproval) -> DestructiveApproval:
        self.verify(source)
        if not isinstance(approval, DestructiveApproval):
            raise CommandError("destructive_confirmation_required")
        expected = _mac(self._secret, approval.identity_payload())
        if not hmac.compare_digest(expected, str(approval.mac or "")):
            raise CommandError("invalid_destructive_approval")
        if approval.source_hash != source.source_hash:
            raise CommandError("destructive_source_mismatch")
        return approval
