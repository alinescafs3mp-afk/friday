"""Shared plumbing for the admin routers: authorisation, auditing and parsing.

Every admin handler needs the same three things — a capability check, an audit entry,
and coercion of untyped query and body values. They live here so each router module
can import them without importing ``jericho.admin_api``, which imports the routers and
would be a cycle.
"""

from __future__ import annotations

import asyncio
import functools
import hashlib
import json
import secrets
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse

from jericho.diagnostics import collect_diagnostics
from jericho.permissions import LEGACY_OWNER_USER_ID
from jericho.purge import purge_knowledge
from jericho.storage import MAX_API_TOKEN_TTL_SECONDS, validate_user_id
from jericho.storage.models import AuditEntry, InboxStatus, ResolutionStatus, new_id


def _services(request: Request):
    return request.app.state


def _require(request: Request, capability: str):
    actor = request.state.actor
    request.app.state.auth_service.require(actor, capability)
    return actor


def _json_value(value: Any, default: Any) -> Any:
    if value in (None, ""):
        return default
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


async def _request_json(request: Request) -> dict[str, Any]:
    """Parse a bounded Admin API JSON object with consistent client errors."""

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


def _parse_float(value: Any, *, field: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=f"{field} must be a number") from exc
    if parsed != parsed or parsed in {float("inf"), float("-inf")}:
        raise HTTPException(status_code=400, detail=f"{field} must be finite")
    return parsed


def _parse_unit_float(value: Any, *, field: str) -> float:
    parsed = _parse_float(value, field=field)
    if not 0.0 <= parsed <= 1.0:
        raise HTTPException(status_code=400, detail=f"{field} must be between 0 and 1")
    return parsed


def _parse_bool(value: Any, *, field: str) -> bool:
    if not isinstance(value, bool):
        raise HTTPException(status_code=400, detail=f"{field} must be a boolean")
    return value


def _parse_int(value: Any, *, field: str) -> int:
    if isinstance(value, bool):
        raise HTTPException(status_code=400, detail=f"{field} must be an integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=f"{field} must be an integer") from exc
    if isinstance(value, float) and not value.is_integer():
        raise HTTPException(status_code=400, detail=f"{field} must be an integer")
    if isinstance(value, str) and str(parsed) != value.strip():
        raise HTTPException(status_code=400, detail=f"{field} must be an integer")
    return parsed


def _audit(
    request: Request,
    action: str,
    target_type: str,
    target_id: str | None,
    *,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
) -> None:
    state = _services(request)
    actor = request.state.actor
    state.storage.log_audit(
        AuditEntry(
            id=new_id("audit"),
            user_id=actor.user_id,
            action=action,
            target_type=target_type,
            target_id=target_id,
            before_json=before,
            after_json=after,
            ip_address=getattr(request.state, "client_ip", ""),
            request_id=getattr(request.state, "request_id", ""),
        )
    )


def _target_user(request: Request, user_id: str | None) -> str:
    return user_id or request.state.actor.user_id


def _audit_cross_tenant_read(request: Request, action: str, target_user: str | None, **detail: Any) -> None:
    """Record an admin reading ANOTHER account's content.

    Same-tenant reads stay unlogged — the owner browsing their own data would
    flood the trail with no privacy signal. Data egress (downloads, exports,
    audit-log reads) is always logged separately regardless of tenant.

    ``target_user=None`` means the route was called without a tenant filter, i.e.
    it read EVERY account. That is strictly more sensitive than reading one
    foreign account, so it is always recorded — the routes that accept an optional
    ``user_id`` were the ones most worth logging and the easiest to overlook.
    """
    if target_user is None:
        _audit(request, action, "user", "*", after={**detail, "scope": "all_tenants"})
        return
    if target_user != request.state.actor.user_id:
        _audit(request, action, "user", target_user, after=detail or None)


def _protect_owner_target(request: Request, user_id: str) -> None:
    """Prevent delegated administrators from mutating an owner account."""

    actor = request.state.actor
    target = _services(request).storage.get_user(user_id)
    target_is_owner = user_id == LEGACY_OWNER_USER_ID or bool(target and target.get("preset_key") == "owner")
    if target_is_owner and not actor.is_owner:
        raise HTTPException(status_code=403, detail="Only an owner may modify an owner account")


def _require_delegable_capability(request: Request, security_id: str) -> None:
    """Require that a non-owner already holds every capability it delegates."""

    state = _services(request)
    if state.auth_service.get_capability(security_id) is None:
        raise HTTPException(status_code=400, detail=f"Unknown capability: {security_id}")
    actor = request.state.actor
    if actor.is_owner:
        return
    if not state.auth_service.authorize(actor, security_id).allowed:
        raise HTTPException(
            status_code=403,
            detail=f"Cannot delegate capability not held by the actor: {security_id}",
        )


def _require_delegable_preset(request: Request, preset_key: str) -> None:
    """Reject preset assignments that would exceed the actor's own authority."""

    state = _services(request)
    if not state.auth_service.preset_exists(preset_key):
        raise HTTPException(status_code=400, detail="Unknown preset")
    actor = request.state.actor
    if preset_key == "owner" and not actor.is_owner:
        raise HTTPException(status_code=403, detail="Only an owner may assign the owner preset")
    preset = next(
        (item for item in state.auth_service.list_presets() if item.get("preset_key") == preset_key),
        None,
    )
    if preset is None:
        raise HTTPException(status_code=400, detail="Unknown preset")
    for security_id in preset.get("capabilities", []):
        _require_delegable_capability(request, str(security_id))


def _safe_runtime_file(root: Path, candidate: str) -> Path:
    resolved_root = root.resolve()
    resolved = Path(candidate).resolve()
    if resolved != resolved_root and resolved_root not in resolved.parents:
        raise HTTPException(status_code=404, detail="File not found")
    if not resolved.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    return resolved


# Imported by the router modules; several are unused inside this file, and
# `ruff --fix` would strip them as dead without the explicit list.
__all__ = [
    "APIRouter",
    "Any",
    "AuditEntry",
    "FileResponse",
    "HTTPException",
    "InboxStatus",
    "LEGACY_OWNER_USER_ID",
    "MAX_API_TOKEN_TTL_SECONDS",
    "Path",
    "Query",
    "Request",
    "ResolutionStatus",
    "_audit",
    "_audit_cross_tenant_read",
    "_json_value",
    "_parse_bool",
    "_parse_float",
    "_parse_int",
    "_parse_unit_float",
    "_protect_owner_target",
    "_request_json",
    "_require",
    "_require_delegable_capability",
    "_require_delegable_preset",
    "_safe_runtime_file",
    "_services",
    "_target_user",
    "asyncio",
    "collect_diagnostics",
    "functools",
    "hashlib",
    "json",
    "new_id",
    "purge_knowledge",
    "secrets",
    "validate_user_id",
]
