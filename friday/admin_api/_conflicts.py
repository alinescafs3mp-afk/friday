"""Admin API: contradictions, duplicates and their resolutions.

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
    ResolutionStatus,
    _audit,
    _audit_cross_tenant_read,
    _protect_owner_target,
    _request_json,
    _require,
    _services,
    _target_user,
    functools,
)
from friday.api.kg import (
    _conflict_audit_fingerprint,
    _merge_audit_fingerprint,
    _public_conflict_card,
    _public_conflict_result,
    _public_duplicate_scan_report,
    _public_merge_history_card,
    _public_merge_result,
)
from friday.storage._graph import _bounded_merge_history_rows, _count_merge_history
from friday.storage._knowledge import _bounded_knowledge_conflict_rows
from friday.workers._blocking import run_blocking

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
    bounded_limit = max(1, min(int(limit), 500))
    bounded_offset = max(0, int(offset))

    def _collect() -> tuple[list[dict[str, Any]], int]:
        rows = _bounded_knowledge_conflict_rows(
            storage,
            user_id,
            status=status or None,
            limit=bounded_limit + 1,
            offset=bounded_offset,
        )
        total = storage.count_knowledge_conflicts(user_id, status=status or None)
        return rows, total

    try:
        rows, total = await run_blocking(_collect)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    items = [_public_conflict_card(item) for item in rows[:bounded_limit]]
    return {
        "user_id": user_id,
        "items": items,
        "count": len(items),
        "total": total,
        "limit": bounded_limit,
        "offset": bounded_offset,
        "matched_at_least": total,
        "truncated": bounded_offset + len(items) < total,
    }


@router.post("/conflicts/bulk-review")
async def bulk_review_conflicts(request: Request) -> dict[str, Any]:
    """Review a bounded conflict batch without hiding per-item failures."""

    _require(request, "admin.all_data.manage")
    body = await _request_json(request)
    user_id = str(body.get("user_id") or "")
    _protect_owner_target(request, user_id)
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
            result = await run_blocking(
                _services(request).kg.review_conflict,
                user_id,
                conflict_id,
                status,
                reviewed_by=request.state.actor.own_id,
                resolution_note=resolution_note,
            )
        except ValueError as exc:
            skipped.append({"id": conflict_id, "reason": str(exc)})
            continue
        if not result:
            skipped.append({"id": conflict_id, "reason": "not_found"})
            continue
        public_result = _public_conflict_result(result)
        changed.append(public_result)
        _audit(
            request,
            f"admin.knowledge_conflict.{status}",
            "knowledge_conflict",
            conflict_id,
            after=_conflict_audit_fingerprint(result),
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
    _protect_owner_target(request, user_id)
    status = str(body.get("status") or "").casefold()
    try:
        result = await run_blocking(
            _services(request).kg.review_conflict,
            user_id,
            conflict_id,
            status,
            reviewed_by=request.state.actor.own_id,
            resolution_note=str(body.get("resolution_note") or "")[:1000],
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not result:
        raise HTTPException(status_code=404, detail="Конфликт знаний не найден")
    _audit(
        request,
        f"admin.knowledge_conflict.{status}",
        "knowledge_conflict",
        conflict_id,
        after=_conflict_audit_fingerprint(result),
    )
    return {"item": _public_conflict_result(result)}


@router.post("/conflicts/{conflict_id}/resolve")
async def resolve_conflict(conflict_id: str, request: Request) -> dict[str, Any]:
    _require(request, "admin.all_data.manage")
    body = await _request_json(request)
    user_id = str(body.get("user_id") or "")
    _protect_owner_target(request, user_id)
    winner_id = str(body.get("winner_id") or "")
    if not winner_id:
        raise HTTPException(status_code=400, detail="Нужен winner_id")
    try:
        result = await run_blocking(
            _services(request).kg.resolve_conflict,
            user_id,
            conflict_id,
            winner_id,
            reviewed_by=request.state.actor.own_id,
            resolution_note=str(body.get("resolution_note") or "")[:1000],
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not result:
        raise HTTPException(status_code=404, detail="Конфликт знаний не найден")
    _audit(
        request,
        "admin.knowledge_conflict.resolve",
        "knowledge_conflict",
        conflict_id,
        after=_conflict_audit_fingerprint(result),
    )
    return {"item": _public_conflict_result(result)}


@router.get("/resolutions")
async def list_resolutions(
    request: Request,
    user_id: str,
    status: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> dict[str, Any]:
    """Очередь слияний — страницей, и страница НАЗЫВАЕТ себя страницей.

    Хранилище умеет и лимит, и смещение, и счёт (`count_resolution_candidates`), а
    маршрут не брал ничего: отдавал умолчательные 500 строк и `count`, равный длине
    этой самой страницы. На корпусе владельца кандидатур 45 947 — оператор видел
    «500» и не мог отличить его от «всего 500». Числа `count` и `total` тут разные
    по смыслу, поэтому названы по-разному и отдаются оба.

    Два счётчика внутри строки — из той же семьи: `knowledge_count` считался как
    `len(get_entity_knowledge(..., limit=1000))`, то есть у сущности с 45 000
    документов показывал ровно 1000, а стоил 500 строк × 2 сущности × выборку в
    тысячу записей. Теперь это COUNT в базе.

    Вся сборка уходит с event loop: даже страница в сотню строк — это четыре сотни
    обращений к SQLite, а один uvicorn обслуживает и API, и мост Telegram, и все
    органы из одного цикла.
    """
    _require(request, "admin.all_data.read")
    _audit_cross_tenant_read(request, "admin.resolutions.read", user_id)
    try:
        status_enum = ResolutionStatus(status) if status else None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Недопустимый статус объединения") from exc
    state = _services(request)
    bounded_limit = max(1, min(int(limit), 500))
    bounded_offset = max(0, int(offset))

    def _collect() -> dict[str, Any]:
        items = state.kg.resolver.get_resolutions(
            user_id,
            status_enum,
            limit=bounded_limit,
            offset=bounded_offset,
        )
        total = state.storage.count_resolution_candidates(user_id, status_enum)
        return {
            "user_id": user_id,
            "items": items,
            "count": len(items),
            "total": total,
            "limit": bounded_limit,
            "offset": bounded_offset,
            "matched_at_least": int(getattr(items, "matched_at_least", len(items))),
            "truncated": bounded_offset + len(items) < total,
        }

    return await run_blocking(_collect)


@router.post("/knowledge/detect-duplicates")
async def detect_knowledge_duplicates(request: Request) -> dict[str, Any]:
    _require(request, "admin.all_data.manage")
    body = await _request_json(request)
    target = _target_user(request, str(body.get("user_id") or "") or None)
    _protect_owner_target(request, target)
    if not _services(request).storage.get_user(target):
        raise HTTPException(status_code=404, detail="Пользователь не найден")
    from friday.dedup import detect_near_duplicates

    state = _services(request)
    # Off the event loop: a manual scan on a large corpus would otherwise block every
    # other request. ``full_rescan`` lets an operator re-walk history on demand.
    result = await run_blocking(
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
    _protect_owner_target(request, user_id)
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
    report = await run_blocking(
        functools.partial(_services(request).kg.resolver.sweep_duplicates, user_id, min_confidence=0.55)
    )
    public_report = _public_duplicate_scan_report(report)
    _audit(request, "admin.entity_resolution.detect", "user", user_id, after=public_report)
    return {"user_id": user_id, **public_report}


@router.post("/resolutions/{candidate_id}/accept")
async def accept_resolution(candidate_id: str, request: Request) -> dict[str, Any]:
    _require(request, "admin.all_data.manage")
    body = await _request_json(request)
    user_id = str(body.get("user_id") or "")
    _protect_owner_target(request, user_id)
    try:
        merged = await run_blocking(
            _services(request).kg.resolver.accept_resolution,
            candidate_id,
            user_id,
            target_entity_id=body.get("target_entity_id"),
            resolved_by=request.state.actor.own_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _audit(
        request,
        "admin.entity.merge",
        "resolution",
        candidate_id,
        after=_merge_audit_fingerprint(merged),
    )
    return {"entity": _public_merge_result(merged)}


@router.post("/resolutions/{candidate_id}/reject")
async def reject_resolution(candidate_id: str, request: Request) -> dict[str, Any]:
    _require(request, "admin.all_data.manage")
    body = await _request_json(request)
    user_id = str(body.get("user_id") or "")
    _protect_owner_target(request, user_id)
    ok = _services(request).kg.resolver.reject_resolution(
        candidate_id,
        user_id,
        resolved_by=request.state.actor.own_id,
    )
    if not ok:
        raise HTTPException(status_code=404, detail="Кандидат на объединение не найден")
    _audit(request, "admin.entity.merge_rejected", "resolution", candidate_id)
    return {"status": "rejected"}


@router.get("/merges")
async def list_admin_merges(request: Request, user_id: str, limit: int = 50) -> dict[str, Any]:
    _require(request, "admin.all_data.read")
    _audit_cross_tenant_read(request, "admin.merges.read", user_id)
    storage = _services(request).storage
    bounded = max(1, min(int(limit), 200))
    rows = await run_blocking(
        _bounded_merge_history_rows,
        storage,
        user_id,
        limit=bounded + 1,
    )
    total = await run_blocking(_count_merge_history, storage, user_id)
    items = [_public_merge_history_card(row) for row in rows[:bounded]]
    return {
        "user_id": user_id,
        "items": items,
        "count": len(items),
        "total": total,
        "matched_at_least": total,
        "truncated": total > len(items),
    }


@router.post("/merges/{merge_id}/undo")
async def undo_admin_merge(merge_id: str, request: Request) -> dict[str, Any]:
    _require(request, "admin.all_data.manage")
    body = await _request_json(request)
    user_id = str(body.get("user_id") or "")
    _protect_owner_target(request, user_id)
    if not _services(request).storage.get_user(user_id):
        raise HTTPException(status_code=404, detail="Пользователь не найден")
    try:
        result = await run_blocking(
            _services(request).kg.resolver.unmerge,
            user_id,
            merge_id,
            undone_by=request.state.actor.user_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _audit(
        request,
        "admin.entity.unmerge",
        "merge",
        merge_id,
        after=_merge_audit_fingerprint(result),
    )
    return {"result": _public_merge_result(result)}
