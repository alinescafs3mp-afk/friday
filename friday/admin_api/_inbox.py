"""Admin API: the review queue: classification, bulk actions and advice.

Lifted verbatim out of the single 1965-line module: same paths, methods,
signatures and bodies. The router carries no prefix — ``friday.admin_api``
owns ``/api/admin`` and the order these modules are included in.
"""

from __future__ import annotations

from fastapi import APIRouter, Response

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
from friday.diagnostics.runtime_lease import ProcessLease, RuntimeLeaseError
from friday.secondary_brain.document_map_evidence import (
    DocumentMapShadowOneShotReplayError,
    DocumentMapShadowOneShotUnavailable,
    consume_document_map_shadow_rollout_attestation,
    run_document_map_shadow_one_shot,
)
from friday.secondary_product_witness import (
    SECONDARY_PRODUCT_BACKUP_LEASE_FILENAME,
    SECONDARY_PRODUCT_BACKUP_LEASE_PROTOCOL,
    is_secondary_product_witness_raw,
    issue_secondary_product_advice_proof,
    secondary_product_canonical,
    secondary_product_current_server_identity,
)
from friday.storage._intake import checkpoint_secondary_product_witness_wal

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
    try:
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
            knowledge_kind=(str(body["knowledge_kind"]) if body.get("knowledge_kind") is not None else None),
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail="Не удалось обработать элемент входящих: проверьте параметры",
        ) from exc
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
    existing = state.storage.get_inbox_item(inbox_id, user_id)
    raw = (
        state.storage.get_raw_object(str(existing.get("raw_object_id") or ""), user_id)
        if isinstance(existing, dict)
        else None
    )
    product_witness = is_secondary_product_witness_raw(raw)
    if product_witness and (
        not request.state.actor.is_owner or request.state.actor.identity_id != "owner-token"
    ):
        raise HTTPException(
            status_code=403,
            detail="Secondary product witness доступен только владельцу",
        )
    observer = body.get("secondary_product_observer")
    if product_witness and not isinstance(observer, dict):
        raise HTTPException(
            status_code=400,
            detail="Требуются данные наблюдателя проверки второго контура",
        )
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
    if product_witness:
        assert isinstance(raw, dict) and isinstance(observer, dict)
        try:
            result["secondary_product_advice_proof"] = issue_secondary_product_advice_proof(
                state.storage,
                raw=raw,
                result=result,
                observer=observer,
                settings=state.settings,
                secondary=state.secondary_brain,
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=400,
                detail="Некорректные данные проверки второго контура",
            ) from exc
        except RuntimeError as exc:
            raise HTTPException(
                status_code=503,
                detail="Не удалось проверить состояние второго контура",
            ) from exc
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


@router.post("/secondary-product-witness/purge")
async def purge_secondary_witness(request: Request) -> dict[str, Any]:
    """Delete only one exact, pending secondary product probe after certification."""

    actor = _require(request, "admin.all_data.manage")
    if not actor.is_owner or actor.identity_id != "owner-token":
        raise HTTPException(status_code=403, detail="Secondary product witness доступен только владельцу")
    body = await _request_json(request)
    advice_proof = body.get("advice_proof")
    operation = body.get("operation")
    state = _services(request)

    def purge(*, attested: bool) -> dict[str, Any]:
        return state.storage.purge_secondary_product_witness(
            actor.user_id,
            stage=str(body.get("stage") or ""),
            expected_source_ref_sha256=str(body.get("source_ref_sha256") or ""),
            expected_content_sha256=str(body.get("content_sha256") or ""),
            expected_uploader=actor.own_id,
            cleanup_token=str(body.get("cleanup_token") or ""),
            advice_proof=(advice_proof if attested and isinstance(advice_proof, dict) else None),
            operation=operation if attested and isinstance(operation, dict) else None,
            current_server_identity=(
                secondary_product_current_server_identity(
                    state.settings,
                    state.secondary_brain,
                )
                if attested and (advice_proof is not None or operation is not None)
                else None
            ),
        )

    try:
        with ProcessLease(
            state.settings.state_dir / SECONDARY_PRODUCT_BACKUP_LEASE_FILENAME,
            protocol=SECONDARY_PRODUCT_BACKUP_LEASE_PROTOCOL,
        ):
            result = purge(attested=True)
            try:
                checkpoint_secondary_product_witness_wal(state.storage)
            except RuntimeError:
                if result.get("server_rollout_attestation") is not None:
                    purge(attested=False)
                    checkpoint_secondary_product_witness_wal(state.storage)
                raise
    except (OSError, RuntimeLeaseError) as exc:
        raise HTTPException(
            status_code=503,
            detail="Secondary product witness временно заблокирован снимком базы",
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail="Некорректные данные очистки проверки второго контура",
        ) from exc
    except RuntimeError as exc:
        raise HTTPException(
            status_code=503,
            detail="Не удалось безопасно завершить очистку проверки второго контура",
        ) from exc
    _audit(
        request,
        "admin.inbox.purge_secondary_witness",
        "inbox",
        None,
        after={
            "purged": True,
            "attestation_issued": result.get("server_rollout_attestation") is not None,
        },
    )
    return result


@router.post("/secondary-product-witness/consume-rollout-attestation")
async def consume_secondary_witness_rollout_attestation(request: Request) -> Response:
    """Atomically burn one server-origin rollout witness before operator mutation."""

    actor = _require(request, "admin.all_data.manage")
    if not actor.is_owner or actor.identity_id != "owner-token":
        raise HTTPException(status_code=403, detail="Secondary product witness доступен только владельцу")
    body = await _request_json(request)
    try:
        result = _services(request).storage.consume_secondary_product_rollout_attestation(
            actor.user_id,
            request_value=body,
            current_server_identity=secondary_product_current_server_identity(
                _services(request).settings,
                _services(request).secondary_brain,
            ),
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail="Некорректное подтверждение перехода второго контура",
        ) from exc
    except RuntimeError as exc:
        raise HTTPException(
            status_code=409,
            detail="Подтверждение перехода второго контура уже использовано или изменилось",
        ) from exc
    _audit(
        request,
        "admin.inbox.consume_secondary_product_rollout_attestation",
        "inbox",
        None,
        after={
            "status": "consumed",
            "stage": result.get("stage"),
            "transition": result.get("transition"),
            "request_sha256": result.get("request_sha256"),
        },
    )
    return Response(content=secondary_product_canonical(result), media_type="application/json")


@router.post("/secondary-document-map-witness/consume-rollout-attestation")
async def consume_secondary_document_map_rollout_attestation(request: Request) -> Response:
    """Atomically burn one natural document-map shadow receipt before promotion."""

    actor = _require(request, "admin.all_data.manage")
    if not actor.is_owner or actor.identity_id != "owner-token":
        raise HTTPException(status_code=403, detail="Document-map witness доступен только владельцу")
    body = await _request_json(request)
    try:
        result = consume_document_map_shadow_rollout_attestation(
            _services(request).storage,
            actor.user_id,
            request_value=body,
            settings=_services(request).settings,
            secondary=_services(request).secondary_brain,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail="Некорректное подтверждение document-map shadow",
        ) from exc
    except RuntimeError as exc:
        raise HTTPException(
            status_code=409,
            detail="Подтверждение document-map shadow уже использовано или изменилось",
        ) from exc
    _audit(
        request,
        "admin.inbox.consume_secondary_document_map_rollout_attestation",
        "inbox",
        None,
        after={
            "status": "consumed",
            "transition": result.get("transition"),
            "request_sha256": result.get("request_sha256"),
        },
    )
    return Response(content=secondary_product_canonical(result), media_type="application/json")


@router.post("/secondary-document-map-witness/observe-shadow")
async def observe_secondary_document_map_shadow(request: Request) -> Response:
    """Run one bodyless, code-owned, same-process promotion observation."""

    actor = _require(request, "admin.all_data.manage")
    if not actor.is_owner or actor.identity_id != "owner-token":
        raise HTTPException(status_code=403, detail="Document-map witness доступен только владельцу")
    if await request.body() != b"":
        raise HTTPException(status_code=400, detail="Document-map witness не принимает тело запроса")
    state = _services(request)
    try:
        result = await run_document_map_shadow_one_shot(
            state.storage,
            owner_user_id=actor.user_id,
            settings=state.settings,
            secondary=state.secondary_brain,
        )
    except DocumentMapShadowOneShotReplayError as exc:
        raise HTTPException(
            status_code=409,
            detail="Document-map shadow observation уже запускалось для этого процесса",
        ) from exc
    except (DocumentMapShadowOneShotUnavailable, ValueError, RuntimeError) as exc:
        raise HTTPException(
            status_code=503,
            detail="Document-map shadow observation не дало promotion-grade подтверждение",
        ) from exc
    _audit(
        request,
        "admin.inbox.observe_secondary_document_map_shadow",
        "inbox",
        None,
        after={
            "status": result.get("status"),
            "receipt_sha256": result.get("receipt_sha256"),
            "server_rollout_attestation_sha256": result.get("server_rollout_attestation_sha256"),
        },
    )
    return Response(content=secondary_product_canonical(result), media_type="application/json")
