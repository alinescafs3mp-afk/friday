"""Shared plumbing for the admin routers: authorisation, auditing and parsing.

Every admin handler needs the same three things — a capability check, an audit entry,
and coercion of untyped query and body values. They live here so each router module
can import them without importing ``friday.admin_api``, which imports the routers and
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

from friday.diagnostics import collect_diagnostics
from friday.id_provenance import mark_verified_id
from friday.permissions import LEGACY_OWNER_USER_ID
from friday.purge import purge_knowledge
from friday.storage import MAX_API_TOKEN_TTL_SECONDS, validate_user_id
from friday.storage.models import AuditEntry, InboxStatus, ResolutionStatus, new_id


def _services(request: Request):
    return request.app.state


def _require(request: Request, capability: str):
    actor = request.state.actor
    request.app.state.auth_service.require(actor, capability)
    return actor


def _require_any(request: Request, *capabilities: str) -> tuple[Any, str]:
    """Allow the route if the actor holds ANY of these, and say which one did it.

    Returned so the caller can vary what it discloses by the authority actually
    used, and so the audit trail records the reason rather than only the fact —
    «прочитал чужой аккаунт» and «посмотрел, сколько тот написал» are different
    events and must not look identical in the log.

    The capabilities are tried in the order given, so the widest one wins when an
    actor holds several; that keeps a full administrator's view unchanged.
    """
    actor = request.state.actor
    service = request.app.state.auth_service
    for capability in capabilities:
        if service.authorize(actor, capability).allowed:
            return actor, capability
    # Re-run the first one so the refusal is raised by the same code path (and with
    # the same shape and audit behaviour) as every other denied admin call.
    service.require(actor, capabilities[0])
    raise AssertionError("unreachable: require() must have raised")  # pragma: no cover


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
        raise HTTPException(status_code=400, detail="Тело запроса должно быть корректным JSON") from exc
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="JSON-тело должно быть объектом")
    request.state.json_body = body
    return body


def _parse_float(value: Any, *, field: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=f"{field}: нужно число") from exc
    if parsed != parsed or parsed in {float("inf"), float("-inf")}:
        raise HTTPException(status_code=400, detail=f"{field}: нужно конечное число")
    return parsed


def _parse_unit_float(value: Any, *, field: str) -> float:
    parsed = _parse_float(value, field=field)
    if not 0.0 <= parsed <= 1.0:
        raise HTTPException(status_code=400, detail=f"{field}: значение от 0 до 1")
    return parsed


def _parse_bool(value: Any, *, field: str) -> bool:
    if not isinstance(value, bool):
        raise HTTPException(status_code=400, detail=f"{field}: нужно логическое значение")
    return value


def _parse_int(value: Any, *, field: str) -> int:
    if isinstance(value, bool):
        raise HTTPException(status_code=400, detail=f"{field}: нужно целое число")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=f"{field}: нужно целое число") from exc
    if isinstance(value, float) and not value.is_integer():
        raise HTTPException(status_code=400, detail=f"{field}: нужно целое число")
    if isinstance(value, str) and str(parsed) != value.strip():
        raise HTTPException(status_code=400, detail=f"{field}: нужно целое число")
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
            # Кто ДЕЙСТВОВАЛ, а не в чьём архиве. Для админских дорог это тем
            # важнее: здесь чистят базу и читают чужое.
            user_id=actor.own_id,
            action=action,
            target_type=target_type,
            target_id=target_id,
            before_json=before,
            after_json=after,
            ip_address=getattr(request.state, "audit_ip", ""),
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
    actor = request.state.actor
    if target_user is None:
        _audit(request, action, "user", "*", after={**detail, "scope": "all_tenants"})
        return
    # В ОБЩЕМ архиве `user_id` у всех один — это арендатор, — и сравнение по нему
    # давало «читает своё» всегда. Воспроизведено: `_target_user(None)` возвращает
    # арендатора, он равен `actor.user_id`, и запись не появлялась НИКОГДА. То есть
    # ровно там, где людей стало много, надзор за чтением выключался целиком.
    #
    # Отсюда две ветки вместо одной. Материал в общем архиве открыт всем по прямой
    # просьбе владельца, и «Боб прочитал документ Алисы» — не нарушение; но это и
    # не «Боб прочитал своё», а именно чтение общего корпуса админской дорогой, и
    # называется оно своим именем. Молча приравнивать его к своему нельзя: тогда
    # единственный след чтения чужого исчезает.
    if getattr(actor, "shared_tenant", False) and target_user == actor.user_id:
        _audit(request, action, "user", "*", after={**detail, "scope": "shared_archive"})
        return
    if target_user != actor.own_id:
        _audit(request, action, "user", target_user, after=detail or None)


def _protect_owner_target(request: Request, user_id: str) -> None:
    """Prevent delegated administrators from mutating an owner account."""

    actor = request.state.actor
    target = _services(request).storage.get_user(user_id)
    target_is_owner = user_id == LEGACY_OWNER_USER_ID or bool(target and target.get("preset_key") == "owner")
    if target_is_owner and not actor.is_owner:
        raise HTTPException(status_code=403, detail="Только владелец может изменять учётную запись владельца")


def _require_delegable_capability(request: Request, security_id: str) -> None:
    """Require that a non-owner already holds every capability it delegates."""

    state = _services(request)
    if state.auth_service.get_capability(security_id) is None:
        raise HTTPException(status_code=400, detail=f"Неизвестное право: {security_id}")
    actor = request.state.actor
    if actor.is_owner:
        return
    if not state.auth_service.authorize(actor, security_id).allowed:
        raise HTTPException(
            status_code=403,
            detail=f"Нельзя делегировать право, которого нет у исполнителя: {security_id}",
        )


def _require_delegable_preset(request: Request, preset_key: str) -> None:
    """Reject preset assignments that would exceed the actor's own authority."""

    state = _services(request)
    if not state.auth_service.preset_exists(preset_key):
        raise HTTPException(status_code=400, detail="Неизвестный пресет")
    actor = request.state.actor
    if preset_key == "owner" and not actor.is_owner:
        raise HTTPException(status_code=403, detail="Только владелец может назначать пресет владельца")
    preset = next(
        (item for item in state.auth_service.list_presets() if item.get("preset_key") == preset_key),
        None,
    )
    if preset is None:
        raise HTTPException(status_code=400, detail="Неизвестный пресет")
    for security_id in preset.get("capabilities", []):
        _require_delegable_capability(request, str(security_id))


def _safe_runtime_file(root: Path, candidate: str) -> Path:
    resolved_root = root.resolve()
    resolved = Path(candidate).resolve()
    if resolved != resolved_root and resolved_root not in resolved.parents:
        raise HTTPException(status_code=404, detail="Файл не найден")
    if not resolved.is_file():
        raise HTTPException(status_code=404, detail="Файл не найден")
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


def _knowledge_fingerprint(item: dict[str, Any] | None) -> dict[str, Any] | None:
    """Enough to identify a Knowledge Object in the journal, and no content.

    `audit_log` is append-only at the DATABASE level — BEFORE UPDATE and BEFORE
    DELETE triggers `RAISE(ABORT)` — so whatever a route writes here is permanent
    beyond the reach of any later fix, purge or redaction. That makes the body of
    a personal note the last thing that belongs in it. What an investigation
    needs is which object and how big it was; a title or account name is itself
    low-entropy personal content and does not belong in an immutable table.
    """
    if not item:
        return None
    content = str(item.get("content") or "")
    return {
        "id": mark_verified_id(item.get("id")),
        "title_chars": len(str(item.get("title") or "")),
        "knowledge_kind": item.get("knowledge_kind"),
        "lifecycle_stage": item.get("lifecycle_stage"),
        "version": item.get("version"),
        "content_chars": len(content),
        "content_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
        "created_at": item.get("created_at"),
        "updated_at": item.get("updated_at"),
    }
