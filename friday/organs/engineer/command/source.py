"""Authenticated owner-source proofs. Model/web/attachment cannot mint these."""

from __future__ import annotations

import hmac
import secrets
from hashlib import sha256
from typing import Any

from friday.config import env as config_env
from friday.engineer_source_binding import canonical_engineer_source_step_id

from .contracts import (
    ALLOWED_CHANNELS,
    AutonomousDelegation,
    CommandError,
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


def _source_step_id(value: Any) -> str:
    try:
        return canonical_engineer_source_step_id(value)
    except ValueError as exc:
        raise CommandError("invalid_source_step") from exc


class OwnerSourceAuthority:
    """Seals current authenticated source-row identity. Separate from command grants."""

    def __init__(self, secret: bytes) -> None:
        if not isinstance(secret, (bytes, bytearray)) or len(secret) < 32:
            raise CommandError("invalid_source_secret")
        self._secret = bytes(secret)

    @classmethod
    def from_env(cls, name: str = "FRIDAY_ENGINEER_OWNER_SOURCE_SECRET") -> OwnerSourceAuthority:
        raw = config_env(name, "")
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
        source_step_id: str,
        source_hash: str,
        telegram_update_id: str,
        isolation_profile: IsolationProfile,
        idempotency_key: str,
    ) -> OwnerSource:
        if not isinstance(isolation_profile, IsolationProfile):
            raise CommandError("invalid_isolation_profile")
        if isolation_profile not in {
            IsolationProfile.ISOLATED_WORKSPACE,
            IsolationProfile.HOST_USER,
        }:
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
            source_step_id=_source_step_id(source_step_id),
            source_hash=source_hash_text,
            telegram_update_id=_as_token(telegram_update_id, code="invalid_telegram_update", limit=128),
            isolation_profile=isolation_profile,
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
            source_step_id=source.source_step_id,
            source_hash=source.source_hash,
            telegram_update_id=source.telegram_update_id,
            isolation_profile=source.isolation_profile,
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
        _source_step_id(source.source_step_id)
        if source.isolation_profile not in {
            IsolationProfile.ISOLATED_WORKSPACE,
            IsolationProfile.HOST_USER,
        }:
            raise CommandError("invalid_isolation_profile")
        return source

    def delegate_autonomous(
        self,
        source: OwnerSource,
        *,
        expires_at: int,
    ) -> AutonomousDelegation:
        """Seal one explicit MODEL/SHELL/HOST_USER delegation for this source."""

        sealed = self.verify(source)
        if sealed.isolation_profile is not IsolationProfile.HOST_USER:
            raise CommandError("host_user_source_required")
        if isinstance(expires_at, bool) or not isinstance(expires_at, int) or expires_at <= 0:
            raise CommandError("invalid_autonomous_delegation")
        delegation = AutonomousDelegation(
            actor_id=sealed.actor_id,
            tenant_id=sealed.tenant_id,
            conversation_id=sealed.conversation_id,
            channel=sealed.channel,
            source_row_id=sealed.source_row_id,
            source_step_id=sealed.source_step_id,
            source_hash=sealed.source_hash,
            telegram_update_id=sealed.telegram_update_id,
            idempotency_key=sealed.idempotency_key,
            isolation_profile=sealed.isolation_profile,
            expires_at=expires_at,
            nonce=secrets.token_hex(16),
            mac="",
        )
        mac = _mac(self._secret, delegation.identity_payload())
        return AutonomousDelegation(
            actor_id=delegation.actor_id,
            tenant_id=delegation.tenant_id,
            conversation_id=delegation.conversation_id,
            channel=delegation.channel,
            source_row_id=delegation.source_row_id,
            source_step_id=delegation.source_step_id,
            source_hash=delegation.source_hash,
            telegram_update_id=delegation.telegram_update_id,
            idempotency_key=delegation.idempotency_key,
            isolation_profile=delegation.isolation_profile,
            expires_at=delegation.expires_at,
            nonce=delegation.nonce,
            mac=mac,
        )

    def verify_autonomous(
        self,
        source: OwnerSource,
        delegation: AutonomousDelegation,
    ) -> AutonomousDelegation:
        sealed = self.verify(source)
        if not isinstance(delegation, AutonomousDelegation):
            raise CommandError("autonomous_delegation_required")
        expected = _mac(self._secret, delegation.identity_payload())
        if not hmac.compare_digest(expected, str(delegation.mac or "")):
            raise CommandError("invalid_autonomous_delegation")
        expected_source = (
            sealed.actor_id,
            sealed.tenant_id,
            sealed.conversation_id,
            sealed.channel,
            sealed.source_row_id,
            sealed.source_step_id,
            sealed.source_hash,
            sealed.telegram_update_id,
            sealed.idempotency_key,
            sealed.isolation_profile,
        )
        delegated_source = (
            delegation.actor_id,
            delegation.tenant_id,
            delegation.conversation_id,
            delegation.channel,
            delegation.source_row_id,
            delegation.source_step_id,
            delegation.source_hash,
            delegation.telegram_update_id,
            delegation.idempotency_key,
            delegation.isolation_profile,
        )
        if delegated_source != expected_source:
            raise CommandError("autonomous_delegation_source_mismatch")
        if delegation.isolation_profile is not IsolationProfile.HOST_USER:
            raise CommandError("host_user_source_required")
        if not delegation.nonce:
            raise CommandError("invalid_autonomous_delegation")
        return delegation
