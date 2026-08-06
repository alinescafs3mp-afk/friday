"""Admin API: the review queue: classification, bulk actions and advice.

Lifted verbatim out of the single 1965-line module: same paths, methods,
signatures and bodies. The router carries no prefix — ``friday.admin_api``
owns ``/api/admin`` and the order these modules are included in.
"""

from __future__ import annotations

from fastapi import APIRouter

from friday.admin_api._deps import (
    Any,
    HTTPException,
    InboxStatus,
    Query,
    Request,
    _audit,
    _audit_cross_tenant_read,
    _json_value,
    _parse_bool,
    _parse_unit_float,
    _protect_owner_target,
    _request_json,
    _require,
    _services,
    _target_user,
)

router = APIRouter()


@router.get("/inbox/groups")
async def group_inbox(
    request: Request,
    user_id: str | None = None,
    by: str = "extension",
) -> dict[str, Any]:
    """Cut the pending queue into groups so one decision can cover many items.

    Read-only. Each group carries the ids of its members, which the caller passes to
    ``/inbox/bulk`` — the endpoint that already refuses to create knowledge. Reviewing
    an import means recognising the noise and dismissing it wholesale; what remains is
    small enough to read one item at a time, which is where promotion stays.

    Ids travel with the group rather than being re-resolved from a predicate at commit
    time: the queue moves, and acting on rows the user never saw is the failure mode
    this design exists to avoid.
    """

    _require(request, "admin.all_data.read")
    target = _target_user(request, user_id)
    _audit_cross_tenant_read(request, "admin.inbox.read", target)
    try:
        grouping = _services(request).storage.group_pending_inbox(target, by=by)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    groups = grouping["groups"]
    # `grouped` — сколько материалов в ПОКАЗАННЫХ группах, `pending_total` —
    # сколько их в очереди. Раньше было только первое, под именем, которое
    # страница подписывала как «Группы непроверенного (N)»: при обрезе сотней
    # число молча уменьшалось вместе с ним.
    return {
        "user_id": target,
        "axis": by,
        "axes": list(_services(request).storage.INBOX_GROUP_AXES),
        "groups": groups,
        "grouped": sum(group["total"] for group in groups),
        "groups_shown": len(groups),
        "groups_total": grouping["groups_total"],
        "pending_total": grouping["items_total"],
    }


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
        raise HTTPException(status_code=400, detail="Недопустимый статус входящих") from exc
    state = _services(request)
    items = state.storage.list_inbox(target, status_enum, limit=limit, offset=offset)
    for item in items:
        item["suggested_tags"] = _json_value(item.get("suggested_tags_json"), [])
        item["suggestions"] = _json_value(item.get("suggestions_json"), {})
        raw = state.storage.get_raw_object(str(item.get("raw_object_id") or ""), target)
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
            knowledge = state.storage.get_knowledge_object(str(item["knowledge_object_id"]), target)
            if knowledge:
                item["knowledge_object"] = knowledge
    return {
        "user_id": target,
        "items": items,
        "count": len(items),
        # Counted with the SAME status the listing used, after its validation —
        # a bad status must still be a 400, not a silently unfiltered total.
        "total": state.storage.count_inbox(target, status_enum),
        "limit": limit,
        "offset": offset,
    }


@router.post("/inbox/{inbox_id}/classify")
async def classify_inbox(inbox_id: str, request: Request) -> dict[str, Any]:
    _require(request, "admin.all_data.manage")
    body = await _request_json(request)
    user_id = str(body.get("user_id") or "")
    _protect_owner_target(request, user_id)
    try:
        status = InboxStatus(str(body.get("status") or "classified"))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Недопустимый статус входящих") from exc
    result = _services(request).ingestion.classify_inbox_item(
        user_id,
        inbox_id,
        status,
        entity_id=body.get("entity_id"),
        tags=body.get("tags") if isinstance(body.get("tags"), list) else None,
        notes=str(body.get("notes") or ""),
        reviewed_by=request.state.actor.own_id,
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
        raise HTTPException(status_code=404, detail="Элемент входящих не найден")
    _audit(request, "admin.inbox.classify", "inbox", inbox_id, after=result)
    return {"item": result}


@router.post("/inbox/bulk")
async def bulk_classify_inbox(request: Request) -> dict[str, Any]:
    """Apply one explicit review outcome to a bounded Inbox selection.

    A bulk action may DISMISS material but never CANONIZE it. Promotion stays
    one-at-a-time, through the review modal that shows the actual content.

    The reason is the invariant, not caution: DATA_LIFECYCLE §3 says nothing becomes a
    Knowledge Object without a human decision, and approving 200 items the reviewer has
    not opened is not a decision. Promotion is also not a status change — it runs
    enrichment, creates graph entities and auto-accepts links above the confidence
    thresholds, then queues relation and conflict candidates. Two hundred of those from
    one click produces more review work than it clears.
    """

    _require(request, "admin.all_data.manage")
    body = await _request_json(request)
    user_id = str(body.get("user_id") or "")
    _protect_owner_target(request, user_id)
    inbox_ids = body.get("inbox_ids")
    if not isinstance(inbox_ids, list) or not inbox_ids:
        raise HTTPException(status_code=400, detail="inbox_ids должен быть непустым списком")
    unique_ids = list(dict.fromkeys(str(item) for item in inbox_ids if str(item).strip()))
    if len(unique_ids) > 200:
        raise HTTPException(
            status_code=400, detail="За один запрос можно разобрать не больше 200 элементов входящих"
        )
    # `status` is required. It used to default to "classified", which — combined with
    # `promote` defaulting to None and `classify_inbox_item` reading
    # `promote is None and status == CLASSIFIED` as consent — meant the MINIMAL request
    # body, naming neither, promoted every item it was given.
    requested_status = str(body.get("status") or "").strip()
    if not requested_status:
        raise HTTPException(status_code=400, detail="Нужен status")
    try:
        status = InboxStatus(requested_status)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Недопустимый статус входящих") from exc
    promote = _parse_bool(body["promote"], field="promote") if "promote" in body else None
    if promote is True or status == InboxStatus.CLASSIFIED:
        raise HTTPException(
            status_code=400,
            detail=(
                "Массовое действие не может создавать знания. Продвигайте материалы "
                "по одному через разбор — там виден исходный текст."
            ),
        )
    changed: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    for inbox_id in unique_ids:
        try:
            result = _services(request).ingestion.classify_inbox_item(
                user_id,
                inbox_id,
                status,
                reviewed_by=request.state.actor.own_id,
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
    _protect_owner_target(request, user_id)
    state = _services(request)
    if not state.storage.get_user(user_id):
        raise HTTPException(status_code=404, detail="Пользователь не найден")
    if not state.llm.enabled:
        raise HTTPException(status_code=503, detail="Локальная модель отключена")
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
