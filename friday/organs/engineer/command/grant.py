"""HMAC grants that never mint from inventory, PATH, documents or model output."""

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
    VerifiedCommandGrant,
    canonical_json_bytes,
)


def _now() -> int:
    return int(time.time())


def _as_nonempty(value: Any, *, code: str, limit: int) -> str:
    text = str(value or "").strip()
    if not text or len(text) > limit or "\x00" in text:
        raise CommandError(code)
    return text


class CommandGrantAuthority:
    def __init__(self, secret: bytes, *, clock: Any = _now) -> None:
        if not isinstance(secret, (bytes, bytearray)) or len(secret) < 32:
            raise CommandError("invalid_grant_secret")
        self._secret = bytes(secret)
        self._clock = clock
        self._used_nonces: dict[str, int] = {}
        self._revoked_nonces: dict[str, int] = {}

    @classmethod
    def from_env(cls, name: str = "FRIDAY_ENGINEER_COMMAND_GRANT_SECRET") -> CommandGrantAuthority:
        raw = os.environ.get(name, "")
        if not raw:
            raise CommandError("grant_secret_missing")
        secret = raw.encode("utf-8") if isinstance(raw, str) else bytes(raw)
        return cls(secret)

    def issue(
        self,
        request: CommandRequest,
        *,
        actor_id: str,
        turn_id: str,
        destructive_confirmed: bool = False,
        ttl_sec: int = GRANT_TTL_DEFAULT_SEC,
    ) -> str:
        if request.origin is not CommandOrigin.OWNER_TURN:
            raise CommandError("owner_origin_required")
        if not isinstance(ttl_sec, int) or isinstance(ttl_sec, bool) or not 1 <= ttl_sec <= GRANT_TTL_MAX_SEC:
            raise CommandError("invalid_grant_ttl")
        now = int(self._clock())
        payload = {
            "actor_id": _as_nonempty(actor_id, code="invalid_actor", limit=128),
            "command_digest": request.digest,
            "destructive_confirmed": bool(destructive_confirmed),
            "exp": now + ttl_sec,
            "iat": now,
            "lane": request.lane.value,
            "nonce": secrets.token_hex(16),
            "origin": request.origin.value,
            "schema": SCHEMA,
            "turn_id": _as_nonempty(turn_id, code="invalid_turn", limit=128),
            "v": 1,
        }
        return self._encode(payload)

    def verify(self, token: str, request: CommandRequest, *, actor_id: str) -> VerifiedCommandGrant:
        payload = self._decode(token)
        now = int(self._clock())
        if payload.get("schema") != SCHEMA or payload.get("v") != 1:
            raise CommandError("invalid_grant")
        if str(payload.get("actor_id") or "") != actor_id:
            raise CommandError("grant_actor_mismatch")
        if str(payload.get("command_digest") or "") != request.digest:
            raise CommandError("grant_command_mismatch")
        if str(payload.get("lane") or "") != request.lane.value:
            raise CommandError("grant_lane_mismatch")
        try:
            origin = CommandOrigin(str(payload.get("origin") or ""))
            lane = CommandLane(str(payload.get("lane") or ""))
        except ValueError as exc:
            raise CommandError("invalid_grant") from exc
        if origin is not CommandOrigin.OWNER_TURN or origin is not request.origin:
            raise CommandError("owner_origin_required")
        exp = int(payload.get("exp") or 0)
        iat = int(payload.get("iat") or 0)
        if iat > now + 5 or exp <= now:
            raise CommandError("grant_expired")
        nonce = str(payload.get("nonce") or "")
        if not nonce:
            raise CommandError("invalid_grant")
        self._gc_nonces(now)
        if nonce in self._used_nonces:
            raise CommandError("grant_replay")
        if nonce in self._revoked_nonces:
            raise CommandError("grant_revoked")
        self._used_nonces[nonce] = exp
        return VerifiedCommandGrant(
            actor_id=str(payload["actor_id"]),
            turn_id=str(payload["turn_id"]),
            command_digest=str(payload["command_digest"]),
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
        if grant.nonce in self._revoked_nonces:
            raise CommandError("grant_revoked")

    def revoke(self, token: str) -> None:
        payload = self._decode(token)
        nonce = str(payload.get("nonce") or "")
        if not nonce:
            raise CommandError("invalid_grant")
        exp = int(payload.get("exp") or 0)
        self._revoked_nonces[nonce] = max(exp, int(self._clock()))

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

    def _gc_nonces(self, now: int) -> None:
        for table in (self._used_nonces, self._revoked_nonces):
            expired = [nonce for nonce, exp in table.items() if exp <= now]
            for nonce in expired:
                table.pop(nonce, None)
