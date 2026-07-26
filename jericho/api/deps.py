"""Request helpers shared by the HTTP routers.

Lifted out of ``server.py`` so a router module can use them without importing the
server back — which would be a cycle. Names keep their leading underscore: they are
internal to the API layer, and keeping them identical made the extraction a pure move
with no call-site churn in server.py.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from fastapi import HTTPException, Request

from jericho.permissions import ActorContext
from jericho.storage.models import AuditEntry, new_id

__all__ = [
    "_audit",
    "_parse_json_bool",
    "_parse_json_float",
    "_request_json",
    "_require",
    "_require_bridge",
    "_json_load",
    "_safe_owned_file",
]


async def _request_json(request: Request) -> dict[str, Any]:
    cached = getattr(request.state, "json_body", None)
    if isinstance(cached, dict):
        return cached
    try:
        body = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=400, detail="Request body must be valid JSON") from exc
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="JSON body must be an object")
    request.state.json_body = body
    return body


def _parse_json_bool(value: Any, *, field: str, default: bool) -> bool:
    """Accept only real JSON booleans for externally visible control flags."""

    if value is None:
        return default
    if not isinstance(value, bool):
        raise HTTPException(status_code=400, detail=f"{field} must be a boolean")
    return value


def _parse_json_float(
    value: Any,
    *,
    field: str,
    default: float,
    minimum: float,
    maximum: float,
) -> float:
    """Parse a finite bounded number and reject booleans/NaN/infinity."""

    if value is None:
        return default
    if isinstance(value, bool):
        raise HTTPException(status_code=400, detail=f"{field} must be a number")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=f"{field} must be a number") from exc
    if not math.isfinite(parsed):
        raise HTTPException(status_code=400, detail=f"{field} must be finite")
    if not minimum <= parsed <= maximum:
        raise HTTPException(
            status_code=400,
            detail=f"{field} must be between {minimum:g} and {maximum:g}",
        )
    return parsed


def _require(request: Request, capability: str) -> ActorContext:
    actor = request.state.actor
    request.app.state.auth_service.require(actor, capability)
    return actor


def _audit(
    request: Request,
    action: str,
    target_type: str,
    target_id: str | None,
    *,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
) -> None:
    request.app.state.storage.log_audit(
        AuditEntry(
            id=new_id("audit"),
            user_id=request.state.actor.user_id,
            action=action,
            target_type=target_type,
            target_id=target_id,
            before_json=before,
            after_json=after,
            ip_address=getattr(request.state, "client_ip", ""),
            request_id=getattr(request.state, "request_id", ""),
        )
    )


def _require_bridge(request: Request) -> ActorContext:
    # The outbound queue is drained only by the Telegram bridge — the sole holder of
    # the bridge secret. Bearer/loopback actors are refused.
    actor = request.state.actor
    if actor.source != "telegram-bridge":
        raise HTTPException(status_code=403, detail="Bridge authentication required")
    return actor


def _json_load(value: Any, default: Any) -> Any:
    if value in (None, ""):
        return default
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


def _safe_owned_file(root: Path, candidate: str) -> Path:
    root = root.resolve()
    path = Path(candidate).resolve()
    if path != root and root not in path.parents:
        raise HTTPException(status_code=404, detail="File not found")
    if not path.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    return path
