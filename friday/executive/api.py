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
    # Автор — ЧЕЛОВЕК, а не канал. Здесь стоял `actor.source`, то есть «api» или
    # «telegram-bridge» (у моста это константа). По этому же полю ищется чат для
    # уведомления и ключуется дедуп миссий: с именем канала уведомление не
    # доходило никому, а в общем архиве просьбы разных людей склеивались бы в одну.
    mission = await executive.create_mission(actor.user_id, goal, created_by=actor.own_id)
    return {"mission": mission}


def _visible_to(actor: Any) -> str | None:
    """Чьи миссии ПОКАЗЫВАТЬ. Всех — таково решение владельца.

    `None` означает «без разбора автора». Владелец 2026-08-04, отвечая на прямой
    вопрос о границах надзора, решил: «все видят всех». Общий архив на то и
    общий — люди работают над одним корпусом и должны видеть, что по нему уже
    идёт, иначе двое запускают одну работу дважды.

    Признак `actor` здесь не участвует намеренно: правило одно для всех, и
    оставленный без дела параметр честнее скрытой ветки, которая говорила бы,
    что различие есть.
    """
    return None


def _controlled_by(actor: Any) -> str | None:
    """Чьи миссии ТРОГАТЬ: свои — или любые, если это хозяин архива.

    Смотреть и управлять — разные права, и одно решение («все видят всех») их не
    объединяет: запустить или остановить чужую работу это вмешательство, а не
    осведомлённость. Пока обе дороги ходили через один помощник, участник вместе
    со списком получал у каждой чужой строки кнопки «Запустить» и «Остановить».
    """
    return None if getattr(actor, "is_owner", False) else actor.own_id


def _authored_by_this_person(mission: dict[str, Any], actor: Any) -> bool:
    """Он ли завёл эту миссию — в любой из двух записей авторства.

    Автор пишется по-разному в зависимости от того, какой дорогой миссия
    заведена: через HTTP это `own_id` (api.py, `create_mission`), а через
    Пятницу — `agent:<own_id>` (execution_kernel, `mission_create`). Сравнение с
    одним только `own_id` объявляло бы чужой ту миссию, которую человек сам
    попросил завести в разговоре, — а таких у участников как раз большинство.
    """
    own = str(getattr(actor, "own_id", "") or "")
    author = str(mission.get("created_by") or "")
    return bool(own) and author in {own, f"agent:{own}"}


def _mission_to_control(request: Request, actor: Any, mission_id: str) -> dict[str, Any]:
    """Миссия, которой этот человек вправе управлять, — или честный отказ.

    Отказы здесь РАЗНЫЕ намеренно. Пока чужие миссии были не видны, «чужая» и
    «несуществующая» обязаны были отвечать одинаково: иначе разница ответов сама
    сообщала бы, что миссия есть. После решения владельца «все видят всех»
    скрывать нечего — чужая строка стоит в списке у всех, — и ответ «не найдена»
    на видимую строку стал бы просто неправдой. Поэтому 404 отвечает только
    отсутствие, а на чужую приходит 403 с причиной.
    """
    executive = request.app.state.executive
    mission = executive.get_mission_view(mission_id, actor.user_id)
    if mission is None:
        raise HTTPException(status_code=404, detail="Mission not found")
    if _controlled_by(actor) is not None and not _authored_by_this_person(mission, actor):
        raise HTTPException(
            status_code=403,
            detail=(
                "Это чужая миссия. Запустить или остановить её может тот, кто её завёл, или владелец архива."
            ),
        )
    return mission


@router.get("")
async def list_missions(
    request: Request,
    status: str | None = Query(default=None),
    limit: int = Query(50, ge=1, le=1000),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    actor = _require(request, "missions.read")
    executive = request.app.state.executive
    # Видно всем — решение владельца. Кнопки «Запустить» и «Остановить» рядом с
    # чужой строкой при этом не работают: управление разведено с показом, см.
    # `_controlled_by`.
    items = executive.list_mission_views(
        actor.user_id,
        status=status,
        limit=limit,
        offset=offset,
        created_by=_visible_to(actor),
    )
    return {"items": items, "count": len(items)}


@router.get("/{mission_id}")
async def get_mission(mission_id: str, request: Request) -> dict[str, Any]:
    actor = _require(request, "missions.read")
    executive = request.app.state.executive
    mission = executive.get_mission_view(mission_id, actor.user_id, created_by=_visible_to(actor))
    if mission is None:
        raise HTTPException(status_code=404, detail="Mission not found")
    return {"mission": mission}


@router.post("/{mission_id}/start")
async def start_mission(mission_id: str, request: Request) -> dict[str, Any]:
    actor = _require(request, "missions.control")
    _mission_to_control(request, actor, mission_id)
    executive = request.app.state.executive
    mission = await executive.start_mission(mission_id, actor.user_id)
    if mission is None:
        raise HTTPException(status_code=404, detail="Mission not found")
    return {"mission": mission}


@router.post("/{mission_id}/stop")
async def stop_mission(mission_id: str, request: Request) -> dict[str, Any]:
    actor = _require(request, "missions.control")
    _mission_to_control(request, actor, mission_id)
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
