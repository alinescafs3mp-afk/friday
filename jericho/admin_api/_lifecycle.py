"""Admin API: stages, deprecation and legacy cleanup.

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
    _audit,
    _audit_cross_tenant_read,
    _json_value,
    _knowledge_fingerprint,
    _parse_bool,
    _parse_int,
    _protect_owner_target,
    _request_json,
    _require,
    _services,
)

router = APIRouter()


@router.get("/cleanup/legacy")
async def preview_legacy_cleanup(
    request: Request,
    user_id: str,
    limit: int = Query(500, ge=1, le=5000),
    offset: int = Query(0, ge=0),
    include_archived: bool = False,
) -> dict[str, Any]:
    _require(request, "admin.all_data.read")
    _audit_cross_tenant_read(request, "admin.cleanup.read", user_id)
    if not _services(request).storage.get_user(user_id):
        raise HTTPException(status_code=404, detail="Пользователь не найден")
    # `scan_legacy_quality_page`, not `scan_legacy_quality`: the suspect predicate reads
    # each object's content and metadata, so there is no SQL COUNT for it — the page and
    # the total have to come from one and the same walk, or the pager lies. The visible
    # consequence is that page one is now genuinely the riskiest, which the «риск»
    # column always implied and the early-exit scan never delivered.
    items, total = _services(request).ingestion.scan_legacy_quality_page(
        user_id,
        limit=limit,
        offset=offset,
        include_archived=include_archived,
    )
    return {
        "user_id": user_id,
        "items": items,
        "count": len(items),
        "total": total,
        "limit": limit,
        "offset": offset,
        "safe_actions": ["return_to_inbox", "reclassify", "keep", "archive", "soft_delete"],
    }


@router.post("/cleanup/legacy/apply")
async def apply_legacy_cleanup(request: Request) -> dict[str, Any]:
    _require(request, "admin.all_data.manage")
    body = await _request_json(request)
    user_id = str(body.get("user_id") or "")
    _protect_owner_target(request, user_id)
    action = str(body.get("action") or "return_to_inbox")
    knowledge_ids = body.get("knowledge_ids")
    if not isinstance(knowledge_ids, list) or not knowledge_ids:
        raise HTTPException(status_code=400, detail="knowledge_ids должен быть непустым списком")
    if len(knowledge_ids) > 200:
        raise HTTPException(status_code=400, detail="За один запрос можно изменить не больше 200 объектов")
    allowed_actions = {"return_to_inbox", "archive", "reclassify", "keep", "soft_delete"}
    if action not in allowed_actions:
        raise HTTPException(status_code=400, detail="Недопустимое действие очистки")
    state = _services(request)
    if not state.storage.get_user(user_id):
        raise HTTPException(status_code=404, detail="Пользователь не найден")
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
            # Отпечаток, а не сама строка: `before` — это весь Knowledge Object
            # вместе с `content`, а журнал append-only на уровне триггеров, и
            # никакой purge потом этот текст не сотрёт. См. `_knowledge_fingerprint`.
            before=_knowledge_fingerprint(before),
            # В `change` лежит `result` целиком, а внутри него — тот же документ с
            # текстом: одной подмены `before` было мало, и это поймал тест.
            after={
                "knowledge_object_id": knowledge_id,
                "status": action,
                "result": _knowledge_fingerprint(result if isinstance(result, dict) else None)
                or {"applied": bool(result)},
            },
        )
    return {
        "user_id": user_id,
        "action": action,
        "changed": changed,
        "changed_count": len(changed),
        "skipped": skipped,
    }


@router.get("/lifecycle/candidates")
async def lifecycle_candidates(
    request: Request,
    user_id: str,
    days_threshold: int = Query(90, ge=1, le=36500),
    limit: int = Query(500, ge=1, le=5000),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    _require(request, "admin.all_data.read")
    _audit_cross_tenant_read(request, "admin.lifecycle.read", user_id)
    storage = _services(request).storage
    items = storage.list_lifecycle_candidates(
        user_id,
        days_threshold=days_threshold,
        limit=limit,
        offset=offset,
    )
    return {
        "user_id": user_id,
        "items": items,
        "count": len(items),
        # This route's whole job is this list, so reporting its page length as the
        # count was the least useful number it could have returned.
        "total": storage.count_lifecycle_candidates(user_id, days_threshold=days_threshold),
        "limit": limit,
        "offset": offset,
        "read_only": True,
    }


@router.post("/lifecycle/apply")
async def apply_lifecycle_review(request: Request) -> dict[str, Any]:
    """Apply an explicit lifecycle decision to selected reviewed candidates."""

    _require(request, "admin.all_data.manage")
    body = await _request_json(request)
    user_id = str(body.get("user_id") or "")
    _protect_owner_target(request, user_id)
    knowledge_ids = body.get("knowledge_ids")
    if not isinstance(knowledge_ids, list) or not knowledge_ids:
        raise HTTPException(status_code=400, detail="knowledge_ids должен быть непустым списком")
    unique_ids = list(dict.fromkeys(str(item) for item in knowledge_ids if str(item).strip()))
    if len(unique_ids) > 200:
        raise HTTPException(
            status_code=400, detail="За раз можно изменить не больше 200 кандидатов жизненного цикла"
        )
    action = str(body.get("action") or "archive").casefold()
    if action not in {"archive", "lower_importance", "keep"}:
        raise HTTPException(status_code=400, detail="Недопустимое действие жизненного цикла")
    days = max(1, min(_parse_int(body.get("days_threshold", 90), field="days_threshold"), 36500))
    state = _services(request)
    # The whole candidate set, walked once — not a 5000-row page of it. The old shape
    # was safe only while the visible table was a prefix of that pool; now that the
    # table pages, an id from a later page would have been rejected as
    # `not_a_current_candidate` while being a perfectly current candidate. Measured on
    # 50000 objects the pool truncated on its own: 8747 true, 2174 returned.
    candidates = {
        str(item["knowledge_object"]["id"]): item
        for item in state.storage.all_lifecycle_candidates(user_id, days_threshold=days)
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
            # Тела документа здесь были ДВАЖДЫ — и до, и после правки.
            before=_knowledge_fingerprint(before),
            after=_knowledge_fingerprint(after),
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
    _audit_cross_tenant_read(request, "admin.lifecycle.read", user_id)
    return {"user_id": user_id, "stages": _services(request).storage.get_lifecycle_stats(user_id)}


@router.post("/lifecycle/deprecate")
async def run_deprecation(request: Request) -> dict[str, Any]:
    """Archive the objects the reviewer selected. `ids` is required.

    It used to sweep every active object under `importance < 0.3` older than the
    threshold, with no selection and none of the protections the read-only
    candidate scan applies — the exact shape of the review-gate bypasses already
    found in `bulk_classify_inbox` and the disk importer, and a direct
    contradiction of DATA_LIFECYCLE §5.
    """
    _require(request, "admin.all_data.manage")
    body = await _request_json(request)
    user_id = str(body.get("user_id") or "")
    _protect_owner_target(request, user_id)
    raw_ids = body.get("ids")
    if not isinstance(raw_ids, list) or not raw_ids:
        raise HTTPException(
            status_code=400,
            detail="Нужны ids: изменения жизненного цикла только для явно выбранных объектов",
        )
    days = max(1, min(_parse_int(body.get("days_threshold", 90), field="days_threshold"), 36500))
    result = _services(request).storage.archive_selected_knowledge(
        user_id, [str(item) for item in raw_ids], days_threshold=days
    )
    _audit(
        request,
        "admin.lifecycle.deprecate",
        "user",
        user_id,
        after={"archived": result["archived"], "skipped": result["skipped"], "days": days},
    )
    # Distinct keys: `**result` also carries `archived` as a LIST, and in a dict
    # literal the later key wins — the count was being overwritten by the ids.
    return {
        "user_id": user_id,
        "archived": len(result["archived"]),
        "archived_ids": result["archived"],
        "skipped": result["skipped"],
    }
