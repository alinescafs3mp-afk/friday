"""HTTP surface for missions: a user router and an admin inspection router.

Both routers follow the existing Friday conventions: capability checks via a
local ``_require`` helper, manual JSON bodies (no pydantic request models), and
plain ``dict`` responses.  Cross-tenant access is confined to the admin router
behind the ``admin.missions.*`` capabilities.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request

from friday.admin_api._deps import _audit, _audit_cross_tenant_read, _protect_owner_target
from friday.permissions import ActorContext

router = APIRouter(prefix="/api/missions", tags=["missions"])
admin_router = APIRouter(prefix="/api/admin/missions", tags=["admin", "missions"])

_MAX_GOAL_CHARS = 4000


def _require(request: Request, capability: str) -> ActorContext:
    actor = request.state.actor
    request.app.state.auth_service.require(actor, capability)
    return actor


async def _body(request: Request) -> dict[str, Any]:
    cached = getattr(request.state, "json_body", None)
    if isinstance(cached, dict):
        return cached
    try:
        data = await request.json()
    except Exception as exc:  # noqa: BLE001 - any parse failure is a 400
        raise HTTPException(status_code=400, detail="Request body must be JSON") from exc
    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail="Request body must be a JSON object")
    return data


@router.post("")
async def create_mission(request: Request) -> dict[str, Any]:
    actor = _require(request, "missions.create")
    body = await _body(request)
    goal = str(body.get("goal") or body.get("message") or "").strip()
    if not goal:
        raise HTTPException(status_code=400, detail="goal is required")
    if len(goal) > _MAX_GOAL_CHARS:
        raise HTTPException(status_code=413, detail="goal is too long")
    executive = request.app.state.executive
    mission = await executive.create_mission(actor.user_id, goal, created_by=actor.source)
    return {"mission": mission}


@router.get("")
async def list_missions(
    request: Request,
    status: str | None = Query(default=None),
    limit: int = Query(50, ge=1, le=1000),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    actor = _require(request, "missions.read")
    executive = request.app.state.executive
    items = executive.list_mission_views(actor.user_id, status=status, limit=limit, offset=offset)
    return {"items": items, "count": len(items)}


@router.get("/{mission_id}")
async def get_mission(mission_id: str, request: Request) -> dict[str, Any]:
    actor = _require(request, "missions.read")
    executive = request.app.state.executive
    mission = executive.get_mission_view(mission_id, actor.user_id)
    if mission is None:
        raise HTTPException(status_code=404, detail="Mission not found")
    return {"mission": mission}


@router.post("/{mission_id}/start")
async def start_mission(mission_id: str, request: Request) -> dict[str, Any]:
    actor = _require(request, "missions.control")
    executive = request.app.state.executive
    mission = await executive.start_mission(mission_id, actor.user_id)
    if mission is None:
        raise HTTPException(status_code=404, detail="Mission not found")
    return {"mission": mission}


@router.post("/{mission_id}/stop")
async def stop_mission(mission_id: str, request: Request) -> dict[str, Any]:
    actor = _require(request, "missions.control")
    executive = request.app.state.executive
    mission = await executive.cancel_mission(mission_id, actor.user_id)
    if mission is None:
        raise HTTPException(status_code=404, detail="Mission not found")
    return {"mission": mission}


@admin_router.get("")
async def admin_list_missions(
    request: Request,
    user_id: str | None = Query(default=None),
    status: str | None = Query(default=None),
    limit: int = Query(200, ge=1, le=5000),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    _require(request, "admin.missions.read")
    _audit_cross_tenant_read(request, "admin.missions.read", user_id)
    executive = request.app.state.executive
    items = executive.list_mission_views(user_id, status=status, limit=limit, offset=offset)
    response: dict[str, Any] = {"items": items, "count": len(items)}
    if user_id is not None:
        response["user_id"] = user_id
    return response


@admin_router.get("/{mission_id}")
async def admin_get_mission(
    mission_id: str,
    request: Request,
    user_id: str | None = Query(default=None),
) -> dict[str, Any]:
    _require(request, "admin.missions.read")
    executive = request.app.state.executive
    mission = executive.get_mission_view(mission_id, user_id)
    if mission is None:
        raise HTTPException(status_code=404, detail="Mission not found")
    # Logged for the same reason the listing above it is: a mission carries the goal
    # the user wrote and the text every step produced, so reading someone else's is
    # reading their content. Only the listing was logged, so the endpoint that shows
    # the actual material was the one that left no trace.
    _audit_cross_tenant_read(request, "admin.missions.read", str(mission.get("user_id") or ""))
    return {"mission": mission}


@admin_router.post("/{mission_id}/cancel")
async def admin_cancel_mission(mission_id: str, request: Request) -> dict[str, Any]:
    _require(request, "admin.missions.manage")
    storage = request.app.state.storage
    owner = storage.get_mission(mission_id)
    if owner is None:
        raise HTTPException(status_code=404, detail="Mission not found")
    target_user = str(owner["user_id"])
    # Cancelling is a cross-tenant MUTATION and was neither guarded nor recorded: a
    # delegated administrator could stop the owner's own missions, and nothing in the
    # audit log said who did it. Every other admin route that touches another account
    # passes through both of these.
    _protect_owner_target(request, target_user)
    executive = request.app.state.executive
    mission = await executive.cancel_mission(mission_id, target_user)
    if mission is None:
        raise HTTPException(status_code=404, detail="Mission not found")
    _audit(request, "admin.mission.cancel", "mission", mission_id, after={"user_id": target_user})
    return {"mission": mission}
