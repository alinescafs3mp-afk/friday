"""Admin API: users, presets, capability overrides and API tokens.

Lifted verbatim out of the single 1965-line module: same paths, methods,
signatures and bodies. The router carries no prefix — ``jericho.admin_api``
owns ``/api/admin`` and the order these modules are included in.
"""

from __future__ import annotations

from fastapi import APIRouter

from jericho.admin_api._deps import (
    MAX_API_TOKEN_TTL_SECONDS,
    Any,
    HTTPException,
    Query,
    Request,
    _audit,
    _audit_cross_tenant_read,
    _protect_owner_target,
    _request_json,
    _require,
    _require_any,
    _require_delegable_capability,
    _require_delegable_preset,
    _services,
    hashlib,
    secrets,
    validate_user_id,
)
from jericho.people import resolve_person, unambiguous

router = APIRouter()


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
    return {
        "items": users,
        "count": len(users),
        "total": state.storage.count_users(),
        "limit": limit,
        "offset": offset,
    }


@router.get("/users/resolve")
async def resolve_user_name(request: Request, name: str = Query(min_length=1)) -> dict[str, Any]:
    """Which account is being called `name`? Every candidate, with how it matched.

    Deliberately returns the whole ranked list and an explicit `unambiguous` flag
    rather than a winner: the caller is about to read one person's material, and two
    accounts answering to the same name must reach them as a question.
    """
    _require(request, "admin.users.read")
    rows = _services(request).storage.list_users(limit=5000)
    matches = resolve_person(rows, name)
    winner = unambiguous(matches)
    return {
        "query": name,
        "matches": [match.to_dict() for match in matches],
        "unambiguous": winner.to_dict() if winner else None,
    }


@router.get("/users/{user_id}/activity")
async def user_activity(
    user_id: str,
    request: Request,
    since: str | None = Query(default=None),
    until: str | None = Query(default=None),
    limit: int = Query(200, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    analysis: list[str] = Query(default=[]),
    top: int = Query(10, ge=1, le=50),
) -> dict[str, Any]:
    """What this account wrote and uploaded, newest first, with a summary beside it.

    Two levels reach this route. `admin.all_data.read` sees everything, content
    included — that was the owner's decision. `admin.activity.read` sees when, how
    much, from where and what became of it, and no text at all: the shape is the
    same, the fields a person authored come back empty. Which one answered is
    recorded, because otherwise the trail cannot tell «looked at the volume» from
    «read the correspondence».
    """
    _, granted = _require_any(request, "admin.all_data.read", "admin.activity.read")
    include_content = granted == "admin.all_data.read"
    _audit_cross_tenant_read(
        request,
        "admin.user.activity.read",
        user_id,
        since=since,
        until=until,
        granted_by=granted,
        content="full" if include_content else "redacted",
        analysis=analysis or None,
    )
    storage = _services(request).storage
    payload: dict[str, Any] = {
        "user_id": user_id,
        "content": "full" if include_content else "redacted",
        "summary": storage.user_activity_summary(user_id, since=since, until=until),
        "items": storage.user_activity(
            user_id,
            since=since,
            until=until,
            limit=limit,
            offset=offset,
            include_content=include_content,
        ),
    }
    if analysis:
        try:
            payload["analysis"] = storage.user_activity_analysis(
                user_id,
                since=since,
                until=until,
                analyses=analysis,
                top=top,
                include_content=include_content,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    return payload


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
    _audit_cross_tenant_read(request, "admin.tokens.read", user_id)
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
    # A delegated administrator must not mint a token for an owner account…
    _protect_owner_target(request, user_id)
    # …nor for any account that can do something they cannot. A token IS that
    # account's whole authority, so issuing one is the broadest delegation there
    # is, and it was the only one not carrying the invariant every narrower
    # delegation carries.
    request.app.state.auth_service.require_delegable_account(actor, user_id)
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
    state = _services(request)
    record = state.storage.get_api_token(token_id)
    if not record:
        raise HTTPException(status_code=404, detail="Token not found or already revoked")
    # Minting a token for an owner was already guarded; revoking one was not, and
    # revocation is the more damaging half — a delegated administrator could lock the
    # owner out of their own instance.
    _protect_owner_target(request, str(record.get("user_id") or ""))
    if not state.storage.revoke_api_token(token_id):
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
