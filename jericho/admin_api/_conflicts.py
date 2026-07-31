"""Admin API: contradictions, duplicates and their resolutions.

Lifted verbatim out of the single 1965-line module: same paths, methods,
signatures and bodies. The router carries no prefix — ``jericho.admin_api``
owns ``/api/admin`` and the order these modules are included in.
"""

from __future__ import annotations

from fastapi import APIRouter

from jericho.admin_api._deps import (
    Any,
    HTTPException,
    Query,
    Request,
    ResolutionStatus,
    _audit,
    _audit_cross_tenant_read,
    _json_value,
    _request_json,
    _require,
    _services,
    _target_user,
    asyncio,
    functools,
)

router = APIRouter()


@router.get("/conflicts")
async def list_conflicts(
    request: Request,
    user_id: str,
    status: str | None = "suggested",
    limit: int = Query(500, ge=1, le=5000),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    _require(request, "admin.all_data.read")
    _audit_cross_tenant_read(request, "admin.conflicts.read", user_id)
    storage = _services(request).storage
    try:
        items = storage.list_knowledge_conflicts(
            user_id,
            status=status or None,
            limit=limit,
            offset=offset,
        )
        total = storage.count_knowledge_conflicts(user_id, status=status or None)
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


@router.post("/conflicts/bulk-review")
async def bulk_review_conflicts(request: Request) -> dict[str, Any]:
    """Review a bounded conflict batch without hiding per-item failures."""

    _require(request, "admin.all_data.manage")
    body = await _request_json(request)
    user_id = str(body.get("user_id") or "")
    status = str(body.get("status") or "").casefold()
    conflict_ids = body.get("conflict_ids")
    if status not in {"confirmed", "dismissed", "resolved"}:
        raise HTTPException(status_code=400, detail="Недопустимый статус проверки конфликта")
    if not isinstance(conflict_ids, list) or not conflict_ids:
        raise HTTPException(status_code=400, detail="conflict_ids должен быть непустым списком")
    unique_ids = list(dict.fromkeys(str(item) for item in conflict_ids if str(item).strip()))
    if len(unique_ids) > 200:
        raise HTTPException(status_code=400, detail="За раз можно разобрать не больше 200 конфликтов")
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
        raise HTTPException(status_code=404, detail="Конфликт знаний не найден")
    _audit(request, f"admin.knowledge_conflict.{status}", "knowledge_conflict", conflict_id, after=result)
    return {"item": result}


@router.post("/conflicts/{conflict_id}/resolve")
async def resolve_conflict(conflict_id: str, request: Request) -> dict[str, Any]:
    _require(request, "admin.all_data.manage")
    body = await _request_json(request)
    user_id = str(body.get("user_id") or "")
    winner_id = str(body.get("winner_id") or "")
    if not winner_id:
        raise HTTPException(status_code=400, detail="Нужен winner_id")
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
        raise HTTPException(status_code=404, detail="Конфликт знаний не найден")
    _audit(request, "admin.knowledge_conflict.resolve", "knowledge_conflict", conflict_id, after=result)
    return {"item": result}


@router.get("/resolutions")
async def list_resolutions(request: Request, user_id: str, status: str | None = None) -> dict[str, Any]:
    _require(request, "admin.all_data.read")
    _audit_cross_tenant_read(request, "admin.resolutions.read", user_id)
    try:
        status_enum = ResolutionStatus(status) if status else None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Недопустимый статус объединения") from exc
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
        raise HTTPException(status_code=404, detail="Пользователь не найден")
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
        raise HTTPException(status_code=404, detail="Пользователь не найден")
    # Off the event loop, like the knowledge-duplicate route beside it. This one was
    # synchronous inside an `async def`: on a large graph a manual scan froze every
    # other request for the duration — measured at 137 s for 2000 entities.
    #
    # A budgeted tick, not a full pass, and the answer is the state of the walk.
    # `detect_duplicates` returned the proposals it happened to reach before the
    # pair ceiling, and the fact that it had stopped early existed only as a WARNING
    # in the log — so a short list read as «nothing more to merge».
    report = await asyncio.to_thread(
        functools.partial(_services(request).kg.resolver.sweep_duplicates, user_id, min_confidence=0.55)
    )
    _audit(request, "admin.entity_resolution.detect", "user", user_id, after=report)
    return {"user_id": user_id, **report}


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
        raise HTTPException(status_code=404, detail="Кандидат на объединение не найден")
    _audit(request, "admin.entity.merge_rejected", "resolution", candidate_id)
    return {"status": "rejected"}


@router.get("/merges")
async def list_admin_merges(request: Request, user_id: str, limit: int = 50) -> dict[str, Any]:
    _require(request, "admin.all_data.read")
    _audit_cross_tenant_read(request, "admin.merges.read", user_id)
    items = _services(request).storage.list_merge_history(user_id, limit=max(1, min(int(limit), 200)))
    for item in items:
        item["undoable"] = bool(
            item.get("transfer_json") and item["transfer_json"] not in ("{}", "")
        ) and not item.get("undone_at")
    return {"user_id": user_id, "items": items, "count": len(items)}


@router.post("/merges/{merge_id}/undo")
async def undo_admin_merge(merge_id: str, request: Request) -> dict[str, Any]:
    _require(request, "admin.all_data.manage")
    body = await _request_json(request)
    user_id = str(body.get("user_id") or "")
    if not _services(request).storage.get_user(user_id):
        raise HTTPException(status_code=404, detail="Пользователь не найден")
    try:
        result = _services(request).kg.resolver.unmerge(
            user_id,
            merge_id,
            undone_by=request.state.actor.user_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _audit(request, "admin.entity.unmerge", "merge", merge_id, after=result)
    return {"result": result}
