"""Admin API: entities, the graph view and relation candidates.

Lifted verbatim out of the single 1965-line module: same paths, methods,
signatures and bodies. The router carries no prefix — ``friday.admin_api``
owns ``/api/admin`` and the order these modules are included in.
"""

from __future__ import annotations

from fastapi import APIRouter

from friday.admin_api._deps import (
    Any,
    HTTPException,
    Query,
    Request,
    _audit,
    _audit_cross_tenant_read,
    _json_value,
    _protect_owner_target,
    _request_json,
    _require,
    _services,
    _target_user,
    asyncio,
    functools,
)

router = APIRouter()


@router.get("/entities")
async def list_all_entities(
    request: Request,
    user_id: str | None = None,
    entity_type: str | None = None,
    limit: int = Query(500, ge=1, le=5000),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    _require(request, "admin.all_data.read")
    target = _target_user(request, user_id)
    if entity_type:
        from friday.storage.models import EntityType

        try:
            parsed_type = EntityType(entity_type)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Недопустимый тип сущности") from exc
    else:
        parsed_type = None
    _audit_cross_tenant_read(request, "admin.entities.read", target)
    storage = _services(request).storage
    items = storage.list_entities(target, parsed_type, limit=limit, offset=offset)
    return {
        "user_id": target,
        "items": items,
        "count": len(items),
        # A real total, from the same filters — `count` is len(items) and on a full
        # page equals the limit, which the screen could not tell from «that is all».
        "total": storage.count_entities(target, parsed_type),
        "limit": limit,
        "offset": offset,
    }


@router.post("/entities")
async def create_entity_admin(request: Request) -> dict[str, Any]:
    _require(request, "admin.all_data.manage")
    body = await _request_json(request)
    target = _target_user(request, str(body.get("user_id") or "") or None)
    _protect_owner_target(request, target)
    from friday.storage.models import EntityType

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
    _protect_owner_target(request, target)
    state = _services(request)
    before = state.kg.get_entity(entity_id, target)
    if not before:
        raise HTTPException(status_code=404, detail="Сущность не найдена")
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
    _protect_owner_target(request, user_id)
    state = _services(request)
    before = state.kg.get_entity(entity_id, user_id)
    if not before or not state.kg.delete_entity(user_id, entity_id):
        raise HTTPException(status_code=404, detail="Сущность не найдена")
    _audit(request, "admin.entity.delete", "entity", entity_id, before=before)
    return {"status": "soft_deleted"}


@router.get("/graph")
async def graph_overview(
    request: Request,
    user_id: str,
    limit: int = Query(120, ge=10, le=500),
    entity_types: str = "",
    relation_types: str = "",
    only_relations: bool = False,
    min_weight: int = Query(1, ge=1, le=50),
    min_confidence: float = Query(0.0, ge=0.0, le=1.0),
    as_of: str = "",
    search: str = "",
    hide_isolates: bool = False,
) -> dict[str, Any]:
    """Связная картина графа целиком — то, что можно нарисовать.

    Off the event loop: на большом графе это соединение таблицы связей с самой
    собой, а соседний маршрут подграфа уже стоил пяти минут блокировки, когда
    считался синхронно.

    Фильтры приходят строками через запятую и сужают ОТБОР узлов в запросе, а не
    рисование: отобрав сто самых связанных сущностей и отсеяв их в браузере, вид
    показал бы «людей — трое» там, где людей в архиве четыре тысячи.
    """
    _require(request, "admin.all_data.read")
    _audit_cross_tenant_read(request, "admin.graph.read", user_id, scope="overview")
    try:
        return await asyncio.to_thread(
            functools.partial(
                _services(request).storage.graph_overview,
                user_id,
                limit=limit,
                entity_types=[item for item in entity_types.split(",") if item.strip()],
                relation_types=[item for item in relation_types.split(",") if item.strip()],
                only_relations=only_relations,
                min_weight=min_weight,
                min_confidence=min_confidence,
                as_of=as_of,
                search=search,
                hide_isolates=hide_isolates,
            )
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/graph/{entity_id}")
async def graph(
    entity_id: str,
    request: Request,
    user_id: str,
    depth: int = Query(2, ge=0, le=5),
    entity_types: str = "",
    relation_types: str = "",
    min_weight: float = Query(0.0, ge=0.0, le=1.0),
    min_confidence: float = Query(0.0, ge=0.0, le=1.0),
    as_of: str = "",
) -> dict[str, Any]:
    """Окрестность узла — с теми же фильтрами, что и общий вид.

    До этого маршрут не принимал НИ ОДНОГО фильтра: человек выбирал «только
    люди», переключался с общей картины на окрестность узла и молча получал всё.
    Молча — худшая часть: вид выглядел отфильтрованным.

    `as_of` показывает картину на дату; отменённая с тех пор связь в неё
    возвращается, потому что тогда она была верна.
    """

    _require(request, "admin.all_data.read")
    _audit_cross_tenant_read(request, "admin.graph.read", user_id, entity_id=entity_id, depth=depth)
    try:
        return _services(request).kg.get_entity_graph(
            user_id,
            entity_id,
            depth,
            as_of=as_of,
            entity_types=[item for item in entity_types.split(",") if item.strip()],
            relation_types=[item for item in relation_types.split(",") if item.strip()],
            min_weight=min_weight,
            min_confidence=min_confidence,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/relation-candidates")
async def list_relation_candidates(
    request: Request,
    user_id: str,
    status: str | None = "suggested",
    limit: int = Query(500, ge=1, le=5000),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    _require(request, "admin.all_data.read")
    _audit_cross_tenant_read(request, "admin.relations.read", user_id)
    storage = _services(request).storage
    try:
        items = storage.list_relation_candidates(
            user_id,
            status=status or None,
            limit=limit,
            offset=offset,
        )
        total = storage.count_relation_candidates(user_id, status=status or None)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    for item in items:
        item["evidence"] = _json_value(item.get("evidence_json"), {})
    return {
        "user_id": user_id,
        "items": items,
        "count": len(items),
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.post("/relation-candidates/bulk-review")
async def bulk_review_relation_candidates(request: Request) -> dict[str, Any]:
    """Review a bounded candidate batch while reporting partial failures."""

    _require(request, "admin.all_data.manage")
    body = await _request_json(request)
    user_id = str(body.get("user_id") or "")
    _protect_owner_target(request, user_id)
    status = str(body.get("status") or "").casefold()
    candidate_ids = body.get("candidate_ids")
    if status not in {"accepted", "rejected"}:
        raise HTTPException(status_code=400, detail="status должен быть accepted или rejected")
    if not isinstance(candidate_ids, list) or not candidate_ids:
        raise HTTPException(status_code=400, detail="candidate_ids должен быть непустым списком")
    unique_ids = list(dict.fromkeys(str(item) for item in candidate_ids if str(item).strip()))
    if len(unique_ids) > 200:
        raise HTTPException(status_code=400, detail="За раз можно разобрать не больше 200 кандидатов связей")

    changed: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    for candidate_id in unique_ids:
        try:
            result = _services(request).kg.review_relation_candidate(
                user_id,
                candidate_id,
                status,
                reviewed_by=request.state.actor.own_id,
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


@router.post("/relations/{relation_id}/invalidate")
async def invalidate_relation(relation_id: str, request: Request) -> dict[str, Any]:
    """Связь перестала быть верной — но она БЫЛА, и это остаётся в архиве.

    Отдельно от мягкого удаления: то говорит «этого не было», это — «было и
    кончилось». Дата окончания приходит от человека (`valid_to`), а дата записи
    ставится сама: без второй нельзя восстановить, что система считала верным
    неделю назад, и почему тогда ответила именно так.
    """

    _require(request, "admin.all_data.manage")
    body = await _request_json(request)
    user_id = str(body.get("user_id") or "")
    _protect_owner_target(request, user_id)
    try:
        result = _services(request).kg.invalidate_relation(
            user_id,
            relation_id,
            valid_to=str(body.get("valid_to") or ""),
            superseded_by=str(body.get("superseded_by") or ""),
            reason=str(body.get("reason") or ""),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not result:
        raise HTTPException(status_code=404, detail="Связь не найдена")
    _audit(request, "admin.relation.invalidate", "relation", relation_id, after=result)
    return {"item": result}


@router.post("/relation-candidates/{candidate_id}/review")
async def review_relation_candidate(candidate_id: str, request: Request) -> dict[str, Any]:
    _require(request, "admin.all_data.manage")
    body = await _request_json(request)
    user_id = str(body.get("user_id") or "")
    _protect_owner_target(request, user_id)
    status = str(body.get("status") or "").casefold()
    try:
        result = _services(request).kg.review_relation_candidate(
            user_id,
            candidate_id,
            status,
            reviewed_by=request.state.actor.own_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not result:
        raise HTTPException(status_code=404, detail="Кандидат связи не найден")
    _audit(request, f"admin.relation_candidate.{status}", "relation_candidate", candidate_id, after=result)
    return {"item": result}
