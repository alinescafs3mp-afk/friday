"""HMAC grants bound to an authenticated owner source, never to origin/argv."""

from __future__ import annotations

import hmac
import json
import os
import secrets
import time
from hashlib import sha256
from typing import Any

from .contracts import (
    GRANT_TTL_DEFAULT_SEC,
    GRANT_TTL_MAX_SEC,
    MAX_GRANT_CHARS,
    SCHEMA,
    CommandError,
    CommandLane,
    CommandOrigin,
    CommandRequest,
    DestructiveApproval,
    IsolationProfile,
    OwnerSource,
    VerifiedCommandGrant,
    canonical_json_bytes,
)
from .source import OwnerSourceAuthority
from .store import CommandJobStore


def _now() -> int:
    return int(time.time())


class CommandGrantAuthority:
    def __init__(
        self,
        secret: bytes,
        source_authority: OwnerSourceAuthority,
        *,
        clock: Any = _now,
    ) -> None:
        if not isinstance(secret, (bytes, bytearray)) or len(secret) < 32:
            raise CommandError("invalid_grant_secret")
        self._secret = bytes(secret)
        self.source_authority = source_authority
        self._clock = clock
        self._store: CommandJobStore | None = None

    @classmethod
    def from_env(cls) -> CommandGrantAuthority:
        raw = os.environ.get("FRIDAY_ENGINEER_COMMAND_GRANT_SECRET", "")
        if not raw:
            raise CommandError("grant_secret_missing")
        secret = raw.encode("utf-8") if isinstance(raw, str) else bytes(raw)
        return cls(secret, OwnerSourceAuthority.from_env())

    def bind_store(self, store: CommandJobStore) -> None:
        self._store = store

    def issue(
        self,
        request: CommandRequest,
        *,
        source: OwnerSource,
        destructive_approval: DestructiveApproval | None = None,
        ttl_sec: int = GRANT_TTL_DEFAULT_SEC,
    ) -> str:
        sealed = self.source_authority.verify(source)
        if request.origin is not CommandOrigin.OWNER_TURN:
            raise CommandError("owner_origin_required")
        if request.idempotency_key != sealed.idempotency_key:
            raise CommandError("grant_idempotency_mismatch")
        if not isinstance(ttl_sec, int) or isinstance(ttl_sec, bool) or not 1 <= ttl_sec <= GRANT_TTL_MAX_SEC:
            raise CommandError("invalid_grant_ttl")
        confirmed = False
        approval_mac = ""
        if destructive_approval is not None:
            checked = self.source_authority.verify_destructive(sealed, destructive_approval)
            if checked.command_digest != request.digest:
                raise CommandError("destructive_digest_mismatch")
            confirmed = True
            approval_mac = checked.mac
        now = int(self._clock())
        payload = {
            "actor_id": sealed.actor_id,
            "argv_sha256": request.argv_sha256,
            "channel": sealed.channel,
            "command_digest": request.digest,
            "conversation_id": sealed.conversation_id,
            "destructive_approval_mac": approval_mac,
            "destructive_confirmed": confirmed,
            "exp": now + ttl_sec,
            "host_user_authorized": sealed.host_user_authorized,
            "iat": now,
            "idempotency_key": sealed.idempotency_key,
            "isolation_profile": sealed.isolation_profile.value,
            "lane": request.lane.value,
            "nonce": secrets.token_hex(16),
            "origin": request.origin.value,
            "schema": SCHEMA,
            "source_hash": sealed.source_hash,
            "source_row_id": sealed.source_row_id,
            "telegram_update_id": sealed.telegram_update_id,
            "tenant_id": sealed.tenant_id,
            "v": 2,
        }
        return self._encode(payload)

    def inspect(self, token: str) -> dict[str, Any]:
        return self._decode(token)

    def parse(
        self,
        token: str,
        request: CommandRequest,
        *,
        actor_id: str,
    ) -> VerifiedCommandGrant:
        payload = self._decode(token)
        now = int(self._clock())
        if payload.get("schema") != SCHEMA or payload.get("v") != 2:
            raise CommandError("invalid_grant")
        if str(payload.get("actor_id") or "") != actor_id:
            raise CommandError("grant_actor_mismatch")
        if str(payload.get("command_digest") or "") != request.digest:
            raise CommandError("grant_command_mismatch")
        if str(payload.get("argv_sha256") or "") != request.argv_sha256:
            raise CommandError("grant_command_mismatch")
        if str(payload.get("lane") or "") != request.lane.value:
            raise CommandError("grant_lane_mismatch")
        if str(payload.get("idempotency_key") or "") != request.idempotency_key:
            raise CommandError("grant_idempotency_mismatch")
        if request.origin is not CommandOrigin.OWNER_TURN:
            raise CommandError("owner_origin_required")
        if str(payload.get("origin") or "") != request.origin.value:
            raise CommandError("owner_origin_required")
        try:
            lane = CommandLane(str(payload.get("lane") or ""))
            origin = CommandOrigin(str(payload.get("origin") or ""))
            isolation = IsolationProfile(str(payload.get("isolation_profile") or ""))
        except ValueError as exc:
            raise CommandError("invalid_grant") from exc
        exp = int(payload.get("exp") or 0)
        iat = int(payload.get("iat") or 0)
        if iat > now + 5 or exp <= now:
            raise CommandError("grant_expired")
        nonce = str(payload.get("nonce") or "")
        if not nonce:
            raise CommandError("invalid_grant")
        return VerifiedCommandGrant(
            actor_id=str(payload["actor_id"]),
            tenant_id=str(payload["tenant_id"]),
            conversation_id=str(payload["conversation_id"]),
            channel=str(payload["channel"]),
            source_row_id=str(payload["source_row_id"]),
            source_hash=str(payload["source_hash"]),
            telegram_update_id=str(payload["telegram_update_id"]),
            isolation_profile=isolation,
            host_user_authorized=bool(payload.get("host_user_authorized")),
            idempotency_key=str(payload["idempotency_key"]),
            command_digest=str(payload["command_digest"]),
            argv_sha256=str(payload["argv_sha256"]),
            lane=lane,
            origin=origin,
            destructive_confirmed=bool(payload.get("destructive_confirmed")),
            expires_at=exp,
            nonce=nonce,
        )

    def still_valid(self, grant: VerifiedCommandGrant) -> None:
        now = int(self._clock())
        if grant.expires_at <= now:
            raise CommandError("grant_expired")
        if self._store is not None and self._store.nonce_revoked(grant.nonce):
            raise CommandError("grant_revoked")

    def revoke(self, token: str) -> None:
        payload = self._decode(token)
        nonce = str(payload.get("nonce") or "")
        if not nonce:
            raise CommandError("invalid_grant")
        if self._store is None:
            raise CommandError("grant_store_unbound")
        exp = int(payload.get("exp") or 0)
        self._store.revoke_nonce(nonce, exp=max(exp, int(self._clock())))

    def sign_receipt(self, payload: dict[str, Any]) -> str:
        body = dict(payload)
        body.pop("receipt_mac", None)
        return hmac.new(self._secret, canonical_json_bytes(body), sha256).hexdigest()

    def _encode(self, payload: dict[str, Any]) -> str:
        body = canonical_json_bytes(payload)
        sig = hmac.new(self._secret, body, sha256).hexdigest()
        token = f"{body.decode('ascii')}.{sig}"
        if len(token) > MAX_GRANT_CHARS:
            raise CommandError("grant_too_large")
        return token

    def _decode(self, token: str) -> dict[str, Any]:
        if not isinstance(token, str) or len(token) > MAX_GRANT_CHARS or "." not in token:
            raise CommandError("invalid_grant")
        body, _, sig = token.rpartition(".")
        expected = hmac.new(self._secret, body.encode("ascii"), sha256).hexdigest()
        if not hmac.compare_digest(expected, sig):
            raise CommandError("invalid_grant")
        try:
            payload = json.loads(body)
        except Exception as exc:
            raise CommandError("invalid_grant") from exc
        if not isinstance(payload, dict):
            raise CommandError("invalid_grant")
        return payload
