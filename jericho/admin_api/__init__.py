"""Authenticated administration and cross-tenant data-management API."""

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

router = APIRouter(prefix="/api/admin", tags=["admin"])


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


def _audit_cross_tenant_read(request: Request, action: str, target_user: str, **detail: Any) -> None:
    """Record an admin reading ANOTHER account's content.

    Same-tenant reads stay unlogged — the owner browsing their own data would
    flood the trail with no privacy signal. Data egress (downloads, exports,
    audit-log reads) is always logged separately regardless of tenant.
    """
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


@router.get("/overview")
async def overview(request: Request) -> dict[str, Any]:
    _require(request, "admin.diagnostics")
    storage = _services(request).storage
    counts: dict[str, int] = {}
    for table in (
        "users",
        "raw_objects",
        "knowledge_objects",
        "inbox",
        "entities",
        "relations",
        "conversations",
        "messages",
        "feedback",
        "feedback_state",
        "relation_candidates",
        "knowledge_conflicts",
    ):
        # ``table`` comes only from the fixed diagnostic allowlist above.
        row = storage.execute(f"SELECT COUNT(*) AS count FROM {table}").fetchone()  # nosec B608
        counts[table] = int(row["count"] if row else 0)
    pending = storage.execute("SELECT COUNT(*) AS count FROM inbox WHERE status='pending'").fetchone()
    # Onboarding hints surface only while there is nothing to review yet, so an
    # empty install shows next steps instead of empty tables.
    bootstrap = (
        _services(request).kg.get_bootstrap_suggestions(request.state.actor.user_id)
        if counts["knowledge_objects"] == 0
        else []
    )
    return {
        "counts": counts,
        "pending_inbox": int(pending["count"] if pending else 0),
        "backups": storage.list_backups()[:5],
        "database": storage.diagnostics(),
        "bootstrap_suggestions": bootstrap,
    }


@router.get("/settings")
async def settings_info(request: Request) -> dict[str, Any]:
    _require(request, "admin.diagnostics")
    return _services(request).settings.public_dict()


@router.get("/diagnostics")
async def diagnostics(request: Request, check_llm: bool = False) -> dict[str, Any]:
    _require(request, "admin.diagnostics")
    state = _services(request)
    return collect_diagnostics(state.settings, state.storage, check_llm_port=check_llm)


@router.get("/users")
async def list_users(
    request: Request,
    limit: int = Query(500, ge=1, le=5000),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    _require(request, "admin.users.read")
    state = _services(request)
    users = state.storage.list_users(limit=limit, offset=offset)
    for user in users:
        user["permission_overrides"] = state.storage.get_permission_overrides(user["id"])
    return {"items": users, "count": len(users)}


@router.post("/users")
async def create_user(request: Request) -> dict[str, Any]:
    actor = _require(request, "admin.users.manage")
    body = await _request_json(request)
    try:
        user_id = validate_user_id(str(body.get("id") or ""))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    preset_key = str(body.get("preset_key") or "user")
    state = _services(request)
    _protect_owner_target(request, user_id)
    _require_delegable_preset(request, preset_key)
    before = state.storage.get_user(user_id)
    user = state.storage.ensure_user(
        user_id,
        source=str(body.get("source") or "admin"),
        external_id=str(body.get("external_id") or ""),
        display_name=str(body.get("display_name") or ""),
        username=str(body.get("username") or ""),
        preset_key=preset_key,
        metadata=body.get("metadata") if isinstance(body.get("metadata"), dict) else {},
    )
    state.auth_service.set_user_preset(user_id, preset_key, acting_actor=actor)
    user = state.storage.get_user(user_id) or user
    _audit(request, "admin.user.upsert", "user", user_id, before=before, after=user)
    return {"user": user, "created_by": actor.user_id}


@router.get("/tokens")
async def list_tokens(
    request: Request,
    user_id: str | None = None,
    include_revoked: bool = False,
) -> dict[str, Any]:
    _require(request, "admin.tokens.manage")
    items = _services(request).storage.list_api_tokens(user_id, include_revoked=include_revoked)
    return {"items": items, "count": len(items)}


@router.post("/tokens")
async def create_token(request: Request) -> dict[str, Any]:
    actor = _require(request, "admin.tokens.manage")
    body = await _request_json(request)
    try:
        user_id = validate_user_id(str(body.get("user_id") or ""))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    state = _services(request)
    if not state.storage.get_user(user_id):
        raise HTTPException(status_code=404, detail="User not found")
    # A delegated administrator must not mint a token for an owner account.
    _protect_owner_target(request, user_id)
    label = str(body.get("label") or "")[:200]
    ttl_seconds: int | None = None
    raw_ttl = body.get("ttl_seconds")
    if raw_ttl is not None:
        # bool is an int subclass — reject it explicitly so `true` is not read as 1s.
        if isinstance(raw_ttl, bool):
            raise HTTPException(status_code=400, detail="ttl_seconds must be an integer")
        try:
            ttl_seconds = int(raw_ttl)
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="ttl_seconds must be an integer") from exc
        # Range-check before minting so a huge value is a 400, not a timedelta OverflowError (500).
        if ttl_seconds <= 0 or ttl_seconds > MAX_API_TOKEN_TTL_SECONDS:
            raise HTTPException(
                status_code=400,
                detail=f"ttl_seconds must be between 1 and {MAX_API_TOKEN_TTL_SECONDS}",
            )
    secret = "jrc_" + secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(secret.encode("utf-8")).hexdigest()
    record = state.storage.create_api_token(
        user_id, token_hash, label=label, created_by=actor.user_id, ttl_seconds=ttl_seconds
    )
    _audit(
        request,
        "admin.token.create",
        "api_token",
        str(record.get("id") or ""),
        after={"user_id": user_id, "label": label, "expires_at": record.get("expires_at")},
    )
    # The plaintext token is returned exactly once and is never stored.
    return {
        "token": secret,
        "id": record.get("id"),
        "user_id": user_id,
        "label": label,
        "expires_at": record.get("expires_at"),
    }


@router.delete("/tokens/{token_id}")
async def revoke_token(token_id: str, request: Request) -> dict[str, Any]:
    _require(request, "admin.tokens.manage")
    if not _services(request).storage.revoke_api_token(token_id):
        raise HTTPException(status_code=404, detail="Token not found or already revoked")
    _audit(request, "admin.token.revoke", "api_token", token_id)
    return {"status": "revoked"}


@router.patch("/users/{user_id}")
async def update_user(user_id: str, request: Request) -> dict[str, Any]:
    _require(request, "admin.users.manage")
    state = _services(request)
    before = state.storage.get_user(user_id)
    if not before:
        raise HTTPException(status_code=404, detail="User not found")
    _protect_owner_target(request, user_id)
    body = await _request_json(request)
    updates: dict[str, Any] = {}
    for field in ("display_name", "username"):
        if field in body:
            updates[field] = str(body[field])
    if "status" in body:
        status = str(body["status"])
        if status not in {"active", "disabled"}:
            raise HTTPException(status_code=400, detail="status must be active or disabled")
        updates["status"] = status
    if "metadata" in body:
        if not isinstance(body["metadata"], dict):
            raise HTTPException(status_code=400, detail="metadata must be an object")
        updates["metadata_json"] = body["metadata"]
    if "preset_key" in body:
        preset_key = str(body["preset_key"])
        _require_delegable_preset(request, preset_key)
        updates["preset_key"] = preset_key
    after = state.storage.update_user(user_id, **updates)
    _audit(request, "admin.user.update", "user", user_id, before=before, after=after)
    return {"user": after}


@router.post("/users/{user_id}/preset")
async def set_user_preset(user_id: str, request: Request) -> dict[str, Any]:
    _require(request, "admin.users.manage")
    state = _services(request)
    body = await _request_json(request)
    preset_key = str(body.get("preset_key") or "")
    before = state.storage.get_user(user_id)
    if not before:
        raise HTTPException(status_code=404, detail="User not found")
    _protect_owner_target(request, user_id)
    _require_delegable_preset(request, preset_key)
    try:
        state.auth_service.set_user_preset(user_id, preset_key, acting_actor=request.state.actor)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    after = state.storage.get_user(user_id)
    _audit(request, "admin.user.preset", "user", user_id, before=before, after=after)
    return {"user": after}


@router.put("/users/{user_id}/permissions/{security_id}")
async def set_permission_override(user_id: str, security_id: str, request: Request) -> dict[str, Any]:
    _require(request, "admin.users.manage")
    state = _services(request)
    if not state.storage.get_user(user_id):
        raise HTTPException(status_code=404, detail="User not found")
    _protect_owner_target(request, user_id)
    body = await _request_json(request)
    effect = str(body.get("effect") or "")
    # Denial only removes authority.  Allowing a capability, or removing a
    # deny so the preset can grant it again, is delegation and must stay
    # within the actor's own authority.
    if effect != "deny":
        _require_delegable_capability(request, security_id)
    before = state.storage.get_permission_overrides(user_id)
    try:
        if effect == "allow":
            state.auth_service.grant_permission(user_id, security_id, acting_actor=request.state.actor)
        elif effect == "deny":
            state.auth_service.deny_permission(user_id, security_id)
        elif effect in {"", "inherit", "remove"}:
            state.auth_service.revoke_permission(user_id, security_id)
        else:
            raise ValueError("effect must be allow, deny, or inherit")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    after = state.storage.get_permission_overrides(user_id)
    _audit(request, "admin.permission.override", "user", user_id, before=before, after=after)
    return {"user_id": user_id, "overrides": after}


@router.get("/capabilities")
async def list_capabilities(request: Request) -> dict[str, Any]:
    _require(request, "admin.users.read")
    items = [capability.__dict__ for capability in _services(request).auth_service.list_capabilities()]
    return {"items": items, "count": len(items)}


@router.get("/presets")
async def list_presets(request: Request) -> dict[str, Any]:
    _require(request, "admin.users.read")
    items = _services(request).auth_service.list_presets()
    return {"items": items, "count": len(items)}


@router.post("/presets")
async def create_preset(request: Request) -> dict[str, Any]:
    actor = _require(request, "admin.presets.manage")
    body = await _request_json(request)
    capabilities = body.get("capabilities")
    if not isinstance(capabilities, list):
        raise HTTPException(status_code=400, detail="capabilities must be a list")
    requested_capabilities = {str(item) for item in capabilities}
    for security_id in requested_capabilities:
        _require_delegable_capability(request, security_id)
    try:
        preset = _services(request).auth_service.create_custom_preset(
            str(body.get("preset_key") or ""),
            str(body.get("name") or ""),
            requested_capabilities,
            description=str(body.get("description") or ""),
            created_by=actor.user_id,
            acting_actor=actor,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _audit(request, "admin.preset.upsert", "preset", preset.get("preset_key"), after=preset)
    return {"preset": preset}


@router.get("/knowledge")
async def list_all_knowledge(
    request: Request,
    user_id: str | None = None,
    lifecycle_stage: str | None = None,
    tag: str | None = None,
    entity_id: str | None = None,
    limit: int = Query(200, ge=1, le=5000),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    _require(request, "admin.all_data.read")
    target = _target_user(request, user_id)
    _audit_cross_tenant_read(request, "admin.knowledge.read", target)
    items = _services(request).storage.list_knowledge_objects(
        target,
        limit=limit,
        offset=offset,
        lifecycle_stage=lifecycle_stage,
        tag=tag,
        entity_id=entity_id,
    )
    return {"user_id": target, "items": items, "count": len(items)}


# Declared before /knowledge/{knowledge_id} so "tags" never matches the
# path parameter.
@router.get("/knowledge/tags")
async def list_all_knowledge_tags(
    request: Request,
    user_id: str | None = None,
    limit: int = Query(200, ge=1, le=1000),
) -> dict[str, Any]:
    _require(request, "admin.all_data.read")
    target = _target_user(request, user_id)
    items = _services(request).storage.list_knowledge_tags(target, limit=limit)
    return {"user_id": target, "items": items, "count": len(items)}


@router.get("/containers")
async def list_all_containers(request: Request, user_id: str | None = None) -> dict[str, Any]:
    _require(request, "admin.all_data.read")
    target = _target_user(request, user_id)
    items = _services(request).kg.list_containers(target)
    return {"user_id": target, "items": items, "count": len(items)}


@router.post("/containers")
async def create_container_admin(request: Request) -> dict[str, Any]:
    _require(request, "admin.all_data.manage")
    body = await _request_json(request)
    target = _target_user(request, str(body.get("user_id") or "") or None)
    try:
        container = _services(request).kg.create_container(
            target,
            str(body.get("name") or ""),
            kind=str(body.get("kind") or "collection"),
            parent_id=str(body.get("parent_id") or "") or None,
            description=str(body.get("description") or ""),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _audit(request, "admin.container.create", "entity", container.get("id"), after=container)
    return {"container": container}


@router.get("/knowledge/{knowledge_id}")
async def inspect_knowledge(knowledge_id: str, request: Request, user_id: str) -> dict[str, Any]:
    _require(request, "admin.all_data.read")
    state = _services(request)
    item = state.storage.get_knowledge_object(knowledge_id, user_id)
    if not item:
        raise HTTPException(status_code=404, detail="Knowledge object not found")
    _audit_cross_tenant_read(request, "admin.knowledge.inspect", user_id, knowledge_id=knowledge_id)
    raw = state.storage.get_raw_object(item["raw_object_id"], user_id)
    versions = state.storage.list_knowledge_versions(knowledge_id, user_id)
    links = state.storage.list_knowledge_entity_links(
        user_id, knowledge_object_id=knowledge_id, status=None, limit=500
    )
    for link in links:
        link["evidence"] = _json_value(link.get("evidence_json"), {})
        link["entity"] = state.storage.get_entity(link["entity_id"], user_id)
    inbox = state.storage.get_inbox_by_raw(item["raw_object_id"], user_id)
    if inbox:
        inbox["suggestions"] = _json_value(inbox.get("suggestions_json"), {})
    item["metadata"] = _json_value(item.get("metadata_json"), {})
    item["tags"] = _json_value(item.get("tags_json"), [])
    if raw:
        raw["metadata"] = _json_value(raw.get("metadata_json"), {})
    return {
        "item": item,
        "raw_object": raw,
        "versions": versions,
        "entity_links": links,
        "inbox": inbox,
    }


@router.get("/knowledge/{knowledge_id}/diff")
async def knowledge_diff(
    knowledge_id: str,
    request: Request,
    user_id: str,
    from_version: int | None = Query(None, ge=1),
    to_version: int | None = Query(None, ge=1),
) -> dict[str, Any]:
    _require(request, "admin.all_data.read")
    _audit_cross_tenant_read(request, "admin.knowledge.diff", user_id, knowledge_id=knowledge_id)
    result = _services(request).storage.diff_knowledge_versions(
        knowledge_id, user_id, from_version=from_version, to_version=to_version
    )
    if result is None:
        raise HTTPException(status_code=404, detail="No versions to diff")
    return result


@router.post("/knowledge/{knowledge_id}/reenrich")
async def reenrich_knowledge(knowledge_id: str, request: Request) -> dict[str, Any]:
    _require(request, "admin.all_data.manage")
    body = await _request_json(request)
    user_id = str(body.get("user_id") or "")
    state = _services(request)
    before = state.storage.get_knowledge_object(knowledge_id, user_id)
    if not before:
        raise HTTPException(status_code=404, detail="Knowledge object not found")
    try:
        result = state.ingestion.reenrich_knowledge(
            user_id,
            knowledge_id,
            apply=_parse_bool(body.get("apply", False), field="apply"),
            reviewed_by=request.state.actor.user_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if result["applied"]:
        _audit(
            request,
            "admin.knowledge.reenrich",
            "knowledge_object",
            knowledge_id,
            before=before,
            after=result["item"],
        )
    return result


@router.post("/knowledge/{knowledge_id}/entity-links")
async def create_knowledge_entity_link(knowledge_id: str, request: Request) -> dict[str, Any]:
    _require(request, "admin.all_data.manage")
    body = await _request_json(request)
    user_id = str(body.get("user_id") or "")
    entity_id = str(body.get("entity_id") or "")
    status = str(body.get("status") or "accepted")
    if status not in {"suggested", "accepted", "rejected"}:
        raise HTTPException(status_code=400, detail="status must be suggested, accepted, or rejected")
    state = _services(request)
    try:
        link = state.kg.link_knowledge_to_entity(
            knowledge_id,
            entity_id,
            user_id,
            confidence=_parse_unit_float(body.get("confidence", 1.0), field="confidence"),
            evidence=body.get("evidence") if isinstance(body.get("evidence"), dict) else {},
            status=status,
            reviewed_by=request.state.actor.user_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _audit(
        request,
        "admin.knowledge.entity_link.create",
        "knowledge_entity_link",
        link.get("id"),
        after=link,
    )
    return {"link": link}


@router.patch("/entity-links/{link_id}")
async def review_knowledge_entity_link(link_id: str, request: Request) -> dict[str, Any]:
    _require(request, "admin.all_data.manage")
    body = await _request_json(request)
    user_id = str(body.get("user_id") or "")
    status = str(body.get("status") or "")
    try:
        link = _services(request).storage.set_knowledge_entity_link_status(
            link_id,
            user_id,
            status,
            reviewed_by=request.state.actor.user_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not link:
        raise HTTPException(status_code=404, detail="Knowledge/entity link not found")
    _audit(
        request,
        "admin.knowledge.entity_link.review",
        "knowledge_entity_link",
        link_id,
        after=link,
    )
    return {"link": link}


@router.patch("/knowledge/{knowledge_id}")
async def update_knowledge(knowledge_id: str, request: Request) -> dict[str, Any]:
    _require(request, "admin.all_data.manage")
    body = await _request_json(request)
    target = str(body.get("user_id") or "")
    state = _services(request)
    before = state.storage.get_knowledge_object(knowledge_id, target)
    if not before:
        raise HTTPException(status_code=404, detail="Knowledge object not found")
    allowed = {
        "title",
        "summary",
        "content",
        "tags_json",
        "importance",
        "lifecycle_stage",
        "knowledge_kind",
        "quality_score",
        "promotion_score",
        "metadata_json",
    }
    updates = {key: body[key] for key in allowed if key in body}
    try:
        after = state.storage.update_knowledge_fields(knowledge_id, target, **updates)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _audit(request, "admin.knowledge.update", "knowledge_object", knowledge_id, before=before, after=after)
    return {"item": after}


@router.delete("/knowledge/{knowledge_id}")
async def delete_knowledge(knowledge_id: str, request: Request, user_id: str) -> dict[str, Any]:
    _require(request, "admin.all_data.manage")
    state = _services(request)
    before = state.storage.get_knowledge_object(knowledge_id, user_id)
    if not before or not state.storage.soft_delete_knowledge_object(knowledge_id, user_id):
        raise HTTPException(status_code=404, detail="Knowledge object not found")
    after = state.storage.get_knowledge_object(knowledge_id, user_id)
    _audit(request, "admin.knowledge.delete", "knowledge_object", knowledge_id, before=before, after=after)
    return {"status": "soft_deleted"}


@router.get("/data/purgeable")
async def list_purgeable_data(
    request: Request,
    user_id: str | None = None,
    older_than_days: int | None = None,
    limit: int = Query(200, ge=1, le=2000),
) -> dict[str, Any]:
    _require(request, "admin.data.purge")
    state = _services(request)
    days = state.settings.purge_retention_days if older_than_days is None else max(0, int(older_than_days))
    items = state.storage.list_purgeable_knowledge(user_id, older_than_days=days, limit=limit)
    return {"items": items, "older_than_days": days, "count": len(items)}


@router.post("/knowledge/{knowledge_id}/purge")
async def purge_knowledge_endpoint(knowledge_id: str, request: Request, user_id: str) -> dict[str, Any]:
    _require(request, "admin.data.purge")
    _protect_owner_target(request, user_id)
    state = _services(request)
    before = state.storage.get_knowledge_object(knowledge_id, user_id)
    if not before:
        raise HTTPException(status_code=404, detail="Knowledge object not found")
    try:
        report = purge_knowledge(state.storage, state.settings, state.memory_vault, knowledge_id, user_id)
    except ValueError as exc:
        # Not yet soft-deleted: purge requires the two-phase delete first.
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    _audit(request, "admin.knowledge.purge", "knowledge_object", knowledge_id, before=before, after=report)
    return {"status": "purged", "report": report}


@router.get("/inbox")
async def list_all_inbox(
    request: Request,
    user_id: str | None = None,
    status: str | None = None,
    limit: int = Query(200, ge=1, le=1000),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    _require(request, "admin.all_data.read")
    target = _target_user(request, user_id)
    _audit_cross_tenant_read(request, "admin.inbox.read", target)
    try:
        status_enum = InboxStatus(status) if status else None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid inbox status") from exc
    state = _services(request)
    items = state.storage.list_inbox(target, status_enum, limit=limit, offset=offset)
    for item in items:
        item["suggested_tags"] = _json_value(item.get("suggested_tags_json"), [])
        item["suggestions"] = _json_value(item.get("suggestions_json"), {})
        raw = state.storage.get_raw_object(item["raw_object_id"], target)
        if raw:
            item["raw_object"] = {
                "id": raw["id"],
                "source": raw.get("source"),
                "source_ref": raw.get("source_ref"),
                "content_type": raw.get("content_type"),
                "raw_content": raw.get("raw_content", ""),
                "received_at": raw.get("received_at"),
                "metadata": _json_value(raw.get("metadata_json"), {}),
            }
        if item.get("knowledge_object_id"):
            item["knowledge_object"] = state.storage.get_knowledge_object(item["knowledge_object_id"], target)
    return {"user_id": target, "items": items, "count": len(items)}


@router.post("/inbox/{inbox_id}/classify")
async def classify_inbox(inbox_id: str, request: Request) -> dict[str, Any]:
    _require(request, "admin.all_data.manage")
    body = await _request_json(request)
    user_id = str(body.get("user_id") or "")
    try:
        status = InboxStatus(str(body.get("status") or "classified"))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid inbox status") from exc
    result = _services(request).ingestion.classify_inbox_item(
        user_id,
        inbox_id,
        status,
        entity_id=body.get("entity_id"),
        tags=body.get("tags") if isinstance(body.get("tags"), list) else None,
        notes=str(body.get("notes") or ""),
        reviewed_by=request.state.actor.user_id,
        promote=_parse_bool(body["promote"], field="promote") if "promote" in body else None,
        title=str(body["title"]) if body.get("title") is not None else None,
        summary=str(body["summary"]) if body.get("summary") is not None else None,
        importance=(
            _parse_unit_float(body["importance"], field="importance")
            if body.get("importance") is not None
            else None
        ),
        knowledge_kind=str(body["knowledge_kind"]) if body.get("knowledge_kind") is not None else None,
    )
    if not result:
        raise HTTPException(status_code=404, detail="Inbox item not found")
    _audit(request, "admin.inbox.classify", "inbox", inbox_id, after=result)
    return {"item": result}


@router.post("/inbox/bulk")
async def bulk_classify_inbox(request: Request) -> dict[str, Any]:
    """Apply one explicit review outcome to a bounded Inbox selection."""

    _require(request, "admin.all_data.manage")
    body = await _request_json(request)
    user_id = str(body.get("user_id") or "")
    inbox_ids = body.get("inbox_ids")
    if not isinstance(inbox_ids, list) or not inbox_ids:
        raise HTTPException(status_code=400, detail="inbox_ids must be a non-empty list")
    unique_ids = list(dict.fromkeys(str(item) for item in inbox_ids if str(item).strip()))
    if len(unique_ids) > 200:
        raise HTTPException(status_code=400, detail="At most 200 Inbox items may be reviewed per request")
    try:
        status = InboxStatus(str(body.get("status") or "classified"))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid inbox status") from exc
    promote = _parse_bool(body["promote"], field="promote") if "promote" in body else None
    changed: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    for inbox_id in unique_ids:
        try:
            result = _services(request).ingestion.classify_inbox_item(
                user_id,
                inbox_id,
                status,
                reviewed_by=request.state.actor.user_id,
                promote=promote,
                notes=str(body.get("notes") or "bulk Inbox review")[:1000],
            )
        except ValueError as exc:
            skipped.append({"id": inbox_id, "reason": str(exc)})
            continue
        if not result:
            skipped.append({"id": inbox_id, "reason": "not_found"})
            continue
        changed.append(result)
    _audit(
        request,
        "admin.inbox.bulk_classify",
        "user",
        user_id,
        after={
            "status": status.value,
            "promote": promote,
            "changed_ids": [item.get("id") for item in changed],
            "skipped": skipped,
        },
    )
    return {
        "user_id": user_id,
        "status": status.value,
        "changed": changed,
        "changed_count": len(changed),
        "skipped": skipped,
    }


@router.post("/inbox/{inbox_id}/advise")
async def advise_inbox(inbox_id: str, request: Request) -> dict[str, Any]:
    """Ask the local model for bounded, advisory-only Inbox enrichment."""

    _require(request, "admin.all_data.manage")
    body = await _request_json(request)
    user_id = str(body.get("user_id") or "")
    state = _services(request)
    if not state.storage.get_user(user_id):
        raise HTTPException(status_code=404, detail="User not found")
    if not state.llm.enabled:
        raise HTTPException(status_code=503, detail="Local model is disabled")
    try:
        result = await state.ingestion.advise_inbox_item(
            user_id,
            inbox_id,
            llm=state.llm,
            requested_by=request.state.actor.user_id,
            force=_parse_bool(body.get("force", False), field="force"),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    advice = result.get("model_advice") if isinstance(result.get("model_advice"), dict) else {}
    _audit(
        request,
        "admin.inbox.model_advice",
        "inbox",
        inbox_id,
        after={
            "user_id": user_id,
            "model": advice.get("model"),
            "recommended_action": advice.get("recommended_action"),
            "confidence": advice.get("confidence"),
            "advisory_only": True,
            "idempotent_replay": bool(result.get("idempotent_replay")),
        },
    )
    return result


@router.get("/entities")
async def list_all_entities(
    request: Request,
    user_id: str | None = None,
    entity_type: str | None = None,
    limit: int = Query(500, ge=1, le=5000),
) -> dict[str, Any]:
    _require(request, "admin.all_data.read")
    target = _target_user(request, user_id)
    if entity_type:
        from jericho.storage.models import EntityType

        try:
            parsed_type = EntityType(entity_type)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid entity type") from exc
    else:
        parsed_type = None
    _audit_cross_tenant_read(request, "admin.entities.read", target)
    items = _services(request).storage.list_entities(target, parsed_type, limit=limit)
    return {"user_id": target, "items": items, "count": len(items)}


@router.post("/entities")
async def create_entity_admin(request: Request) -> dict[str, Any]:
    _require(request, "admin.all_data.manage")
    body = await _request_json(request)
    target = _target_user(request, str(body.get("user_id") or "") or None)
    from jericho.storage.models import EntityType

    try:
        entity = _services(request).kg.create_entity(
            target,
            str(body.get("name") or ""),
            EntityType(str(body.get("entity_type") or "other")),
            aliases=body.get("aliases") if isinstance(body.get("aliases"), list) else [],
            description=str(body.get("description") or ""),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _audit(request, "admin.entity.create", "entity", entity.get("id"), after=entity)
    return {"entity": entity}


@router.patch("/entities/{entity_id}")
async def update_entity_admin(entity_id: str, request: Request) -> dict[str, Any]:
    _require(request, "admin.all_data.manage")
    body = await _request_json(request)
    target = _target_user(request, str(body.get("user_id") or "") or None)
    state = _services(request)
    before = state.kg.get_entity(entity_id, target)
    if not before:
        raise HTTPException(status_code=404, detail="Entity not found")
    fields = {key: body[key] for key in ("name", "entity_type", "aliases", "description") if key in body}
    try:
        after = state.kg.update_entity(target, entity_id, **fields)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _audit(request, "admin.entity.update", "entity", entity_id, before=before, after=after)
    return {"entity": after}


@router.delete("/entities/{entity_id}")
async def delete_entity_admin(entity_id: str, request: Request, user_id: str) -> dict[str, Any]:
    _require(request, "admin.all_data.manage")
    state = _services(request)
    before = state.kg.get_entity(entity_id, user_id)
    if not before or not state.kg.delete_entity(user_id, entity_id):
        raise HTTPException(status_code=404, detail="Entity not found")
    _audit(request, "admin.entity.delete", "entity", entity_id, before=before)
    return {"status": "soft_deleted"}


@router.get("/graph/{entity_id}")
async def graph(
    entity_id: str, request: Request, user_id: str, depth: int = Query(2, ge=0, le=5)
) -> dict[str, Any]:
    _require(request, "admin.all_data.read")
    return _services(request).kg.get_entity_graph(user_id, entity_id, depth)


@router.get("/relation-candidates")
async def list_relation_candidates(
    request: Request,
    user_id: str,
    status: str | None = "suggested",
    limit: int = Query(500, ge=1, le=5000),
) -> dict[str, Any]:
    _require(request, "admin.all_data.read")
    try:
        items = _services(request).storage.list_relation_candidates(
            user_id,
            status=status or None,
            limit=limit,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    for item in items:
        item["evidence"] = _json_value(item.get("evidence_json"), {})
    return {"user_id": user_id, "items": items, "count": len(items)}


@router.post("/relation-candidates/bulk-review")
async def bulk_review_relation_candidates(request: Request) -> dict[str, Any]:
    """Review a bounded candidate batch while reporting partial failures."""

    _require(request, "admin.all_data.manage")
    body = await _request_json(request)
    user_id = str(body.get("user_id") or "")
    status = str(body.get("status") or "").casefold()
    candidate_ids = body.get("candidate_ids")
    if status not in {"accepted", "rejected"}:
        raise HTTPException(status_code=400, detail="status must be accepted or rejected")
    if not isinstance(candidate_ids, list) or not candidate_ids:
        raise HTTPException(status_code=400, detail="candidate_ids must be a non-empty list")
    unique_ids = list(dict.fromkeys(str(item) for item in candidate_ids if str(item).strip()))
    if len(unique_ids) > 200:
        raise HTTPException(status_code=400, detail="At most 200 relation candidates may be reviewed")

    changed: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    for candidate_id in unique_ids:
        try:
            result = _services(request).kg.review_relation_candidate(
                user_id,
                candidate_id,
                status,
                reviewed_by=request.state.actor.user_id,
            )
        except ValueError as exc:
            skipped.append({"id": candidate_id, "reason": str(exc)})
            continue
        if not result:
            skipped.append({"id": candidate_id, "reason": "not_found"})
            continue
        changed.append(result)
        _audit(
            request,
            f"admin.relation_candidate.{status}",
            "relation_candidate",
            candidate_id,
            after=result,
        )
    return {
        "user_id": user_id,
        "status": status,
        "changed": changed,
        "changed_count": len(changed),
        "skipped": skipped,
    }


@router.post("/relation-candidates/{candidate_id}/review")
async def review_relation_candidate(candidate_id: str, request: Request) -> dict[str, Any]:
    _require(request, "admin.all_data.manage")
    body = await _request_json(request)
    user_id = str(body.get("user_id") or "")
    status = str(body.get("status") or "").casefold()
    try:
        result = _services(request).kg.review_relation_candidate(
            user_id,
            candidate_id,
            status,
            reviewed_by=request.state.actor.user_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not result:
        raise HTTPException(status_code=404, detail="Relation candidate not found")
    _audit(request, f"admin.relation_candidate.{status}", "relation_candidate", candidate_id, after=result)
    return {"item": result}


@router.get("/conflicts")
async def list_conflicts(
    request: Request,
    user_id: str,
    status: str | None = "suggested",
    limit: int = Query(500, ge=1, le=5000),
) -> dict[str, Any]:
    _require(request, "admin.all_data.read")
    try:
        items = _services(request).storage.list_knowledge_conflicts(
            user_id,
            status=status or None,
            limit=limit,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    for item in items:
        item["evidence"] = _json_value(item.get("evidence_json"), {})
    return {"user_id": user_id, "items": items, "count": len(items)}


@router.post("/conflicts/bulk-review")
async def bulk_review_conflicts(request: Request) -> dict[str, Any]:
    """Review a bounded conflict batch without hiding per-item failures."""

    _require(request, "admin.all_data.manage")
    body = await _request_json(request)
    user_id = str(body.get("user_id") or "")
    status = str(body.get("status") or "").casefold()
    conflict_ids = body.get("conflict_ids")
    if status not in {"confirmed", "dismissed", "resolved"}:
        raise HTTPException(status_code=400, detail="Invalid conflict review status")
    if not isinstance(conflict_ids, list) or not conflict_ids:
        raise HTTPException(status_code=400, detail="conflict_ids must be a non-empty list")
    unique_ids = list(dict.fromkeys(str(item) for item in conflict_ids if str(item).strip()))
    if len(unique_ids) > 200:
        raise HTTPException(status_code=400, detail="At most 200 conflicts may be reviewed")
    resolution_note = str(body.get("resolution_note") or "")[:1000]

    changed: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    for conflict_id in unique_ids:
        try:
            result = _services(request).kg.review_conflict(
                user_id,
                conflict_id,
                status,
                reviewed_by=request.state.actor.user_id,
                resolution_note=resolution_note,
            )
        except ValueError as exc:
            skipped.append({"id": conflict_id, "reason": str(exc)})
            continue
        if not result:
            skipped.append({"id": conflict_id, "reason": "not_found"})
            continue
        changed.append(result)
        _audit(
            request,
            f"admin.knowledge_conflict.{status}",
            "knowledge_conflict",
            conflict_id,
            after=result,
        )
    return {
        "user_id": user_id,
        "status": status,
        "changed": changed,
        "changed_count": len(changed),
        "skipped": skipped,
    }


@router.post("/conflicts/{conflict_id}/review")
async def review_conflict(conflict_id: str, request: Request) -> dict[str, Any]:
    _require(request, "admin.all_data.manage")
    body = await _request_json(request)
    user_id = str(body.get("user_id") or "")
    status = str(body.get("status") or "").casefold()
    try:
        result = _services(request).kg.review_conflict(
            user_id,
            conflict_id,
            status,
            reviewed_by=request.state.actor.user_id,
            resolution_note=str(body.get("resolution_note") or "")[:1000],
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not result:
        raise HTTPException(status_code=404, detail="Knowledge conflict not found")
    _audit(request, f"admin.knowledge_conflict.{status}", "knowledge_conflict", conflict_id, after=result)
    return {"item": result}


@router.post("/conflicts/{conflict_id}/resolve")
async def resolve_conflict(conflict_id: str, request: Request) -> dict[str, Any]:
    _require(request, "admin.all_data.manage")
    body = await _request_json(request)
    user_id = str(body.get("user_id") or "")
    winner_id = str(body.get("winner_id") or "")
    if not winner_id:
        raise HTTPException(status_code=400, detail="winner_id is required")
    try:
        result = _services(request).kg.resolve_conflict(
            user_id,
            conflict_id,
            winner_id,
            reviewed_by=request.state.actor.user_id,
            resolution_note=str(body.get("resolution_note") or "")[:1000],
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not result:
        raise HTTPException(status_code=404, detail="Knowledge conflict not found")
    _audit(request, "admin.knowledge_conflict.resolve", "knowledge_conflict", conflict_id, after=result)
    return {"item": result}


@router.get("/resolutions")
async def list_resolutions(request: Request, user_id: str, status: str | None = None) -> dict[str, Any]:
    _require(request, "admin.all_data.read")
    try:
        status_enum = ResolutionStatus(status) if status else None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid resolution status") from exc
    state = _services(request)
    items = state.storage.list_resolution_candidates(user_id, status_enum)
    enriched: list[dict[str, Any]] = []
    for item in items:
        item["evidence"] = _json_value(item.get("evidence_json"), {})
        left = state.storage.get_entity(item["entity_a_id"], user_id)
        right = state.storage.get_entity(item["entity_b_id"], user_id)
        if not left or not right:
            continue
        for entity in (left, right):
            entity["aliases"] = _json_value(entity.get("aliases_json"), [])
            entity["knowledge_count"] = len(
                state.storage.get_entity_knowledge(user_id, entity["id"], limit=1000)
            )
            entity["relation_count"] = len(state.storage.get_entity_relations(entity["id"], user_id))
        enriched.append({**item, "entity_a": left, "entity_b": right})
    return {"user_id": user_id, "items": enriched, "count": len(enriched)}


@router.post("/knowledge/detect-duplicates")
async def detect_knowledge_duplicates(request: Request) -> dict[str, Any]:
    _require(request, "admin.all_data.manage")
    body = await _request_json(request)
    target = _target_user(request, str(body.get("user_id") or "") or None)
    if not _services(request).storage.get_user(target):
        raise HTTPException(status_code=404, detail="User not found")
    from jericho.dedup import detect_near_duplicates

    state = _services(request)
    # Off the event loop: a manual scan on a large corpus would otherwise block every
    # other request. ``full_rescan`` lets an operator re-walk history on demand.
    result = await asyncio.to_thread(
        functools.partial(detect_near_duplicates, full_rescan=bool(body.get("full_rescan"))),
        state.storage,
        state.settings,
        target,
    )
    _audit(request, "admin.knowledge.detect_duplicates", "user", target, after=result)
    return {"user_id": target, **result}


@router.post("/resolutions/detect")
async def detect_resolutions(request: Request) -> dict[str, Any]:
    _require(request, "admin.all_data.manage")
    body = await _request_json(request)
    user_id = str(body.get("user_id") or "")
    if not _services(request).storage.get_user(user_id):
        raise HTTPException(status_code=404, detail="User not found")
    candidates = _services(request).kg.resolver.detect_duplicates(user_id, min_confidence=0.55)
    result = {"user_id": user_id, "suggested": [item.to_row() for item in candidates]}
    _audit(
        request,
        "admin.entity_resolution.detect",
        "user",
        user_id,
        after={"suggested_count": len(candidates)},
    )
    return result


@router.post("/resolutions/{candidate_id}/accept")
async def accept_resolution(candidate_id: str, request: Request) -> dict[str, Any]:
    _require(request, "admin.all_data.manage")
    body = await _request_json(request)
    user_id = str(body.get("user_id") or "")
    try:
        merged = _services(request).kg.resolver.accept_resolution(
            candidate_id,
            user_id,
            target_entity_id=body.get("target_entity_id"),
            resolved_by=request.state.actor.user_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _audit(request, "admin.entity.merge", "resolution", candidate_id, after=merged)
    return {"entity": merged}


@router.post("/resolutions/{candidate_id}/reject")
async def reject_resolution(candidate_id: str, request: Request) -> dict[str, Any]:
    _require(request, "admin.all_data.manage")
    body = await _request_json(request)
    user_id = str(body.get("user_id") or "")
    ok = _services(request).kg.resolver.reject_resolution(
        candidate_id,
        user_id,
        resolved_by=request.state.actor.user_id,
    )
    if not ok:
        raise HTTPException(status_code=404, detail="Resolution candidate not found")
    _audit(request, "admin.entity.merge_rejected", "resolution", candidate_id)
    return {"status": "rejected"}


@router.get("/cleanup/legacy")
async def preview_legacy_cleanup(
    request: Request,
    user_id: str,
    limit: int = Query(500, ge=1, le=5000),
    include_archived: bool = False,
) -> dict[str, Any]:
    _require(request, "admin.all_data.read")
    if not _services(request).storage.get_user(user_id):
        raise HTTPException(status_code=404, detail="User not found")
    items = _services(request).ingestion.scan_legacy_quality(
        user_id,
        limit=limit,
        include_archived=include_archived,
    )
    return {
        "user_id": user_id,
        "items": items,
        "count": len(items),
        "safe_actions": ["return_to_inbox", "reclassify", "keep", "archive", "soft_delete"],
    }


@router.post("/cleanup/legacy/apply")
async def apply_legacy_cleanup(request: Request) -> dict[str, Any]:
    _require(request, "admin.all_data.manage")
    body = await _request_json(request)
    user_id = str(body.get("user_id") or "")
    action = str(body.get("action") or "return_to_inbox")
    knowledge_ids = body.get("knowledge_ids")
    if not isinstance(knowledge_ids, list) or not knowledge_ids:
        raise HTTPException(status_code=400, detail="knowledge_ids must be a non-empty list")
    if len(knowledge_ids) > 200:
        raise HTTPException(status_code=400, detail="At most 200 objects may be changed per request")
    allowed_actions = {"return_to_inbox", "archive", "reclassify", "keep", "soft_delete"}
    if action not in allowed_actions:
        raise HTTPException(status_code=400, detail="Invalid cleanup action")
    state = _services(request)
    if not state.storage.get_user(user_id):
        raise HTTPException(status_code=404, detail="User not found")
    reason = str(body.get("reason") or "legacy quality cleanup")[:500]
    changed: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    for knowledge_id in dict.fromkeys(str(item) for item in knowledge_ids):
        before = state.storage.get_knowledge_object(knowledge_id, user_id)
        if not before or before.get("deleted_at"):
            skipped.append({"id": knowledge_id, "reason": "not_found"})
            continue
        assessment = state.ingestion.assess_existing_knowledge(user_id, before)
        if (
            _parse_bool(body.get("require_suspect", True), field="require_suspect")
            and not assessment["suspect"]
        ):
            skipped.append({"id": knowledge_id, "reason": "not_flagged_by_quality_scan"})
            continue
        try:
            result = state.ingestion.apply_legacy_cleanup(
                user_id,
                knowledge_id,
                action=action,
                reviewed_by=request.state.actor.user_id,
                reason=reason,
            )
        except ValueError as exc:
            skipped.append({"id": knowledge_id, "reason": str(exc)})
            continue
        change = {
            "knowledge_object_id": knowledge_id,
            "status": action,
            "result": result,
        }
        changed.append(change)
        _audit(
            request,
            f"admin.knowledge.cleanup.{action}",
            "knowledge_object",
            knowledge_id,
            before=before,
            after=change,
        )
    return {
        "user_id": user_id,
        "action": action,
        "changed": changed,
        "changed_count": len(changed),
        "skipped": skipped,
    }


@router.get("/conversations")
async def list_all_conversations(
    request: Request,
    user_id: str | None = None,
    include_archived: bool = False,
) -> dict[str, Any]:
    _require(request, "admin.all_data.read")
    target = _target_user(request, user_id)
    _audit_cross_tenant_read(request, "admin.conversations.read", target)
    items = _services(request).storage.list_conversations(
        target,
        include_archived=include_archived,
        limit=1000,
    )
    return {"user_id": target, "items": items, "count": len(items)}


@router.post("/conversations/{conversation_id}/archive")
async def archive_conversation(conversation_id: str, request: Request, user_id: str) -> dict[str, Any]:
    _require(request, "admin.all_data.manage")
    _protect_owner_target(request, user_id)
    body = await _request_json(request)
    archived = body.get("archived")
    archived = True if archived is None else _parse_bool(archived, field="archived")
    state = _services(request)
    updated = state.storage.set_conversation_archived(conversation_id, user_id, archived)
    if not updated:
        raise HTTPException(status_code=404, detail="Conversation not found")
    _audit(
        request,
        "admin.conversation.archive",
        "conversation",
        conversation_id,
        after={"is_archived": updated.get("is_archived")},
    )
    return {"conversation": updated}


@router.delete("/conversations/{conversation_id}")
async def delete_conversation(conversation_id: str, request: Request, user_id: str) -> dict[str, Any]:
    _require(request, "admin.all_data.manage")
    _protect_owner_target(request, user_id)
    state = _services(request)
    before = state.storage.get_conversation(conversation_id, user_id)
    if not before:
        raise HTTPException(status_code=404, detail="Conversation not found")
    report = state.storage.delete_conversation(conversation_id, user_id)
    _audit(
        request,
        "admin.conversation.delete",
        "conversation",
        conversation_id,
        before=before,
        after=report,
    )
    return {"status": "deleted", "report": report}


@router.get("/conversations/{conversation_id}/messages")
async def conversation_messages(
    conversation_id: str,
    request: Request,
    user_id: str,
    limit: int = Query(500, ge=1, le=1000),
) -> dict[str, Any]:
    _require(request, "admin.all_data.read")
    if not _services(request).storage.get_conversation(conversation_id, user_id):
        raise HTTPException(status_code=404, detail="Conversation not found")
    _audit_cross_tenant_read(request, "admin.messages.read", user_id, conversation_id=conversation_id)
    storage = _services(request).storage
    items = storage.get_conversation_messages(
        conversation_id,
        user_id=user_id,
        limit=limit,
    )
    # Surface the stored [K#] → Knowledge Object attribution as a resolved legend so
    # the inspector shows what each answer rested on (titles resolved once, cached).
    title_cache: dict[str, str] = {}
    for item in items:
        metadata = _json_value(item.get("metadata_json"), {})
        if not isinstance(metadata, dict):
            item["insights"] = {}
            continue
        citation_map = metadata.get("knowledge_citations")
        citation_map = citation_map if isinstance(citation_map, dict) else {}
        citations = []
        for label, knowledge_id in citation_map.items():
            knowledge_id = str(knowledge_id)
            if knowledge_id and knowledge_id not in title_cache:
                obj = storage.get_knowledge_object(knowledge_id, user_id)
                title_cache[knowledge_id] = str((obj or {}).get("title") or "")
            citations.append(
                {
                    "label": str(label),
                    "knowledge_id": knowledge_id,
                    "title": title_cache.get(knowledge_id, ""),
                }
            )
        item["insights"] = {
            "answer_grounded": metadata.get("answer_grounded"),
            "verification_status": metadata.get("verification_status"),
            "verified": metadata.get("verified"),
            "citations": citations,
        }
    return {"items": items, "count": len(items)}


@router.get("/files")
async def list_files(
    request: Request,
    user_id: str | None = None,
    limit: int = Query(500, ge=1, le=5000),
) -> dict[str, Any]:
    _require(request, "admin.all_data.read")
    target = _target_user(request, user_id)
    _audit_cross_tenant_read(request, "admin.files.read", target)
    rows = (
        _services(request)
        .storage.execute(
            """SELECT id, user_id, source_ref, metadata_json, received_at, deleted_at
           FROM raw_objects WHERE user_id=? AND content_type='file'
           ORDER BY received_at DESC LIMIT ?""",
            (target, limit),
        )
        .fetchall()
    )
    items = []
    for row in rows:
        item = dict(row)
        item["metadata"] = _json_value(item.pop("metadata_json", "{}"), {})
        items.append(item)
    return {"user_id": target, "items": items, "count": len(items)}


@router.get("/files/{raw_id}/download")
async def download_file(raw_id: str, request: Request, user_id: str):
    _require(request, "admin.all_data.read")
    state = _services(request)
    raw = state.storage.get_raw_object(raw_id, user_id)
    if not raw or raw.get("content_type") != "file":
        raise HTTPException(status_code=404, detail="File not found")
    metadata = _json_value(raw.get("metadata_json"), {})
    path = _safe_runtime_file(state.settings.files_dir, str(metadata.get("stored_path") or ""))
    # Data egress is always audited, unlike ordinary list reads.
    _audit(
        request,
        "admin.file.download",
        "raw_object",
        raw_id,
        after={"target_user_id": user_id, "filename": str(metadata.get("filename") or "")},
    )
    return FileResponse(path, filename=str(metadata.get("filename") or path.name))


@router.get("/audit")
async def audit_log(
    request: Request,
    user_id: str | None = None,
    limit: int = Query(500, ge=1, le=5000),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    _require(request, "admin.audit.read")
    items = _services(request).storage.list_audit_log(user_id, limit=limit, offset=offset)
    # Reading the audit trail is itself an auditable event (who inspected
    # whose history); a plain INSERT, so there is no recursion.
    _audit(
        request,
        "admin.audit.read",
        "audit_log",
        user_id or "*",
        after={"limit": limit, "offset": offset, "returned": len(items)},
    )
    return {"items": items, "count": len(items)}


@router.get("/quality")
async def knowledge_quality_dashboard(request: Request, user_id: str) -> dict[str, Any]:
    """One read-only view of the feedback loop and graph-review pressure."""

    _require(request, "admin.all_data.read")
    state = _services(request)
    if not state.storage.get_user(user_id):
        raise HTTPException(status_code=404, detail="User not found")
    usage = state.storage.execute(
        """SELECT COUNT(*) AS tracked,
                  COALESCE(SUM(retrieval_count), 0) AS retrievals,
                  COALESCE(SUM(answer_count), 0) AS answers,
                  COALESCE(SUM(positive_feedback_count), 0) AS positive,
                  COALESCE(SUM(negative_feedback_count), 0) AS negative
           FROM knowledge_usage WHERE user_id=?""",
        (user_id,),
    ).fetchone()
    feedback_state = state.storage.get_feedback_state(user_id, limit=5000)
    classification = [item for item in feedback_state if item.get("feedback_type") == "classification"]
    pending_inbox = state.storage.list_inbox(user_id, InboxStatus.PENDING, limit=5000)
    lifecycle_candidates = state.storage.list_lifecycle_candidates(user_id, limit=500)
    return {
        "user_id": user_id,
        "graph": state.kg.get_stats(user_id),
        "usage": dict(usage) if usage else {},
        "feedback": {
            "history": state.storage.get_feedback_stats(user_id),
            "current_count": len(feedback_state),
            "classification_current": len(classification),
            "classification_negative": sum(1 for item in classification if float(item.get("score") or 0) < 0),
        },
        "review_pressure": {
            "pending_inbox": len(pending_inbox),
            "relation_candidates": len(state.storage.list_relation_candidates(user_id, limit=5000)),
            "conflicts": len(state.storage.list_knowledge_conflicts(user_id, limit=5000)),
            "lifecycle_candidates": len(lifecycle_candidates),
        },
        "lifecycle_candidates": lifecycle_candidates,
    }


@router.get("/lifecycle/candidates")
async def lifecycle_candidates(
    request: Request,
    user_id: str,
    days_threshold: int = Query(90, ge=1, le=36500),
    limit: int = Query(500, ge=1, le=5000),
) -> dict[str, Any]:
    _require(request, "admin.all_data.read")
    items = _services(request).storage.list_lifecycle_candidates(
        user_id,
        days_threshold=days_threshold,
        limit=limit,
    )
    return {"user_id": user_id, "items": items, "count": len(items), "read_only": True}


@router.get("/eval/search")
async def eval_search(
    request: Request, q: str, user_id: str | None = None, limit: int = Query(15, ge=1, le=50)
) -> dict[str, Any]:
    _require(request, "admin.all_data.read")
    target = _target_user(request, user_id)
    state = _services(request)
    result = await state.hybrid_searcher.search(target, q, limit=limit, kg=state.kg)
    items = [
        {
            "id": hit.get("id"),
            "title": hit.get("title") or "Без названия",
            "knowledge_kind": hit.get("knowledge_kind"),
            "lifecycle_stage": hit.get("lifecycle_stage"),
        }
        for hit in result.get("results", [])
        if hit.get("id")
    ]
    return {"user_id": target, "query": q, "items": items, "count": len(items)}


@router.get("/retrieval/explain")
async def retrieval_explain(
    request: Request, q: str, user_id: str | None = None, limit: int = Query(10, ge=1, le=50)
) -> dict[str, Any]:
    """Explain-trace: why the ranker returned/discarded/ordered candidates for a
    query. Read-only, deterministic (no LLM) — the same HybridSearcher run the
    user sees, with the per-signal breakdown and discard reasons surfaced."""
    _require(request, "admin.all_data.read")
    target = _target_user(request, user_id)
    state = _services(request)
    result = await state.hybrid_searcher.search(target, q, limit=limit, kg=state.kg, explain=True)
    trace = result.get("trace", [])
    discarded = [row for row in trace if row.get("status") == "discarded"]
    return {
        "user_id": target,
        "query": result.get("query", q),
        "limit": limit,
        "returned": result.get("count", 0),
        "candidates": len(trace),
        "discarded": len(discarded),
        "trace": trace,
        "strategy": result.get("strategy", {}),
    }


@router.get("/eval/cases")
async def list_eval_cases(request: Request, user_id: str | None = None) -> dict[str, Any]:
    _require(request, "admin.all_data.read")
    target = _target_user(request, user_id)
    cases = _services(request).storage.list_eval_cases(target)
    return {"user_id": target, "items": cases, "count": len(cases)}


@router.post("/eval/cases")
async def add_eval_case(request: Request) -> dict[str, Any]:
    _require(request, "admin.all_data.manage")
    body = await _request_json(request)
    target = _target_user(request, str(body.get("user_id") or "") or None)
    expected = body.get("expected_ids")
    if not isinstance(expected, list):
        raise HTTPException(status_code=400, detail="expected_ids must be a list")
    try:
        case = _services(request).storage.add_eval_case(
            target,
            str(body.get("query") or ""),
            [str(item) for item in expected],
            note=str(body.get("note") or ""),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _audit(request, "admin.eval.case_add", "eval_case", case.get("id"), after=case)
    return {"case": case}


@router.delete("/eval/cases/{case_id}")
async def delete_eval_case(case_id: str, request: Request, user_id: str) -> dict[str, Any]:
    _require(request, "admin.all_data.manage")
    if not _services(request).storage.delete_eval_case(user_id, case_id):
        raise HTTPException(status_code=404, detail="Eval case not found")
    _audit(request, "admin.eval.case_delete", "eval_case", case_id)
    return {"status": "deleted"}


@router.post("/eval/run")
async def run_eval_now(request: Request) -> dict[str, Any]:
    _require(request, "admin.all_data.read")
    body = await _request_json(request)
    target = _target_user(request, str(body.get("user_id") or "") or None)
    from jericho.eval import run_eval

    state = _services(request)
    report = await run_eval(state.storage, state.embeddings, state.settings, target)
    _audit(
        request,
        "admin.eval.run",
        "user",
        target,
        after={"recall_at_k": report.get("recall_at_k"), "cases": report.get("cases")},
    )
    return {"user_id": target, "report": report}


@router.post("/eval/chunk-ab")
async def compare_chunk_recall_now(request: Request) -> dict[str, Any]:
    """A/B the gold set with and without passage-level recall on THIS corpus."""
    _require(request, "admin.all_data.read")
    body = await _request_json(request)
    target = _target_user(request, str(body.get("user_id") or "") or None)
    from jericho.eval import compare_chunk_recall

    state = _services(request)
    report = await compare_chunk_recall(state.storage, state.embeddings, state.settings, target)
    _audit(
        request,
        "admin.eval.chunk_ab",
        "user",
        target,
        after={"delta": report.get("delta"), "cases": report.get("cases")},
    )
    return {"user_id": target, "report": report}


@router.post("/lifecycle/apply")
async def apply_lifecycle_review(request: Request) -> dict[str, Any]:
    """Apply an explicit lifecycle decision to selected reviewed candidates."""

    _require(request, "admin.all_data.manage")
    body = await _request_json(request)
    user_id = str(body.get("user_id") or "")
    knowledge_ids = body.get("knowledge_ids")
    if not isinstance(knowledge_ids, list) or not knowledge_ids:
        raise HTTPException(status_code=400, detail="knowledge_ids must be a non-empty list")
    unique_ids = list(dict.fromkeys(str(item) for item in knowledge_ids if str(item).strip()))
    if len(unique_ids) > 200:
        raise HTTPException(status_code=400, detail="At most 200 lifecycle candidates may be changed")
    action = str(body.get("action") or "archive").casefold()
    if action not in {"archive", "lower_importance", "keep"}:
        raise HTTPException(status_code=400, detail="Invalid lifecycle action")
    days = max(1, min(_parse_int(body.get("days_threshold", 90), field="days_threshold"), 36500))
    state = _services(request)
    candidates = {
        str(item["knowledge_object"]["id"]): item
        for item in state.storage.list_lifecycle_candidates(user_id, days_threshold=days, limit=5000)
    }
    changed: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    for knowledge_id in unique_ids:
        candidate = candidates.get(knowledge_id)
        before = state.storage.get_knowledge_object(knowledge_id, user_id)
        if not before or before.get("deleted_at"):
            skipped.append({"id": knowledge_id, "reason": "not_found"})
            continue
        if candidate is None and _parse_bool(body.get("require_candidate", True), field="require_candidate"):
            skipped.append({"id": knowledge_id, "reason": "not_a_current_candidate"})
            continue
        if action == "archive":
            after = state.storage.update_knowledge_fields(
                knowledge_id,
                user_id,
                lifecycle_stage="archived",
            )
        elif action == "lower_importance":
            suggested = float((candidate or {}).get("suggested_importance", before.get("importance", 0.5)))
            after = state.storage.update_knowledge_fields(
                knowledge_id,
                user_id,
                importance=max(0.0, min(1.0, suggested)),
            )
        else:
            metadata = _json_value(before.get("metadata_json"), {})
            after = state.storage.update_knowledge_fields(
                knowledge_id,
                user_id,
                metadata_json={
                    **metadata,
                    "lifecycle_review": {
                        "decision": "keep",
                        "reviewed_by": request.state.actor.user_id,
                    },
                },
            )
        if not after:
            skipped.append({"id": knowledge_id, "reason": "update_failed"})
            continue
        changed.append({"id": knowledge_id, "action": action, "item": after})
        _audit(
            request,
            f"admin.lifecycle.{action}",
            "knowledge_object",
            knowledge_id,
            before=before,
            after=after,
        )
    return {
        "user_id": user_id,
        "action": action,
        "changed": changed,
        "changed_count": len(changed),
        "skipped": skipped,
    }


@router.get("/lifecycle")
async def lifecycle_stats(request: Request, user_id: str) -> dict[str, Any]:
    _require(request, "admin.all_data.read")
    return {"user_id": user_id, "stages": _services(request).storage.get_lifecycle_stats(user_id)}


@router.post("/lifecycle/deprecate")
async def run_deprecation(request: Request) -> dict[str, Any]:
    _require(request, "admin.all_data.manage")
    body = await _request_json(request)
    user_id = str(body.get("user_id") or "")
    days = max(1, min(_parse_int(body.get("days_threshold", 90), field="days_threshold"), 36500))
    count = _services(request).storage.deprecate_stale_knowledge(user_id, days)
    _audit(request, "admin.lifecycle.deprecate", "user", user_id, after={"count": count, "days": days})
    return {"user_id": user_id, "archived": count}


@router.get("/feedback")
async def feedback_stats(request: Request, user_id: str) -> dict[str, Any]:
    _require(request, "admin.all_data.read")
    storage = _services(request).storage
    return {
        "user_id": user_id,
        "stats": storage.get_feedback_stats(user_id),
        "current": storage.get_feedback_state(user_id, limit=1000),
    }


@router.get("/backups")
async def list_backups(request: Request) -> dict[str, Any]:
    _require(request, "admin.backup.manage")
    items = _services(request).storage.list_backups()
    return {"items": items, "count": len(items)}


@router.post("/backups")
async def create_backup(request: Request) -> dict[str, Any]:
    _require(request, "admin.backup.manage")
    body = await _request_json(request)
    label = str(body.get("label") or "manual")[:80]
    result = _services(request).storage.create_backup(label=label)
    _audit(request, "admin.backup.create", "backup", result.get("database"), after=result)
    return {"backup": result}


@router.post("/backups/{filename}/verify")
async def verify_backup(filename: str, request: Request) -> dict[str, Any]:
    _require(request, "admin.backup.manage")
    try:
        result = _services(request).storage.verify_backup(filename)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Backup not found") from exc
    _audit(request, "admin.backup.verify", "backup", filename, after=result)
    return {"verification": result}


@router.get("/backups/{filename}/download")
async def download_backup(filename: str, request: Request):
    _require(request, "admin.backup.manage")
    path = _safe_runtime_file(
        _services(request).settings.backups_dir,
        str(_services(request).settings.backups_dir / Path(filename).name),
    )
    # A backup contains every tenant's data — the highest-value egress event.
    _audit(request, "admin.backup.download", "backup", path.name)
    return FileResponse(path, filename=path.name, media_type="application/vnd.sqlite3")


@router.post("/exports")
async def create_export(request: Request) -> dict[str, Any]:
    _require(request, "admin.export")
    body = await _request_json(request)
    user_id = str(body.get("user_id") or request.state.actor.user_id)
    try:
        result = _services(request).storage.export_user(user_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    _audit(request, "admin.export.create", "user", user_id, after=result)
    return {"export": result}


@router.get("/exports/{filename}/download")
async def download_export(filename: str, request: Request):
    _require(request, "admin.export")
    path = _safe_runtime_file(
        _services(request).settings.exports_dir,
        str(_services(request).settings.exports_dir / Path(filename).name),
    )
    # Creation is audited separately; the download is the actual egress event.
    _audit(request, "admin.export.download", "export", path.name)
    return FileResponse(path, filename=path.name, media_type="application/json")
