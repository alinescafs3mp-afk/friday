"""Admin API: conversation listing, archiving and transcripts.

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
    _parse_bool,
    _protect_owner_target,
    _request_json,
    _require,
    _services,
    _target_user,
)

router = APIRouter()


@router.get("/chats")
async def chat_feed(request: Request, limit: int = Query(100, ge=1, le=500)) -> dict[str, Any]:
    """Мир глазами Пятницы: кто ей писал и что скидывал, свежие сверху.

    Заказ владельца — видеть переписку так, как её видит она сама. Сводки по
    людям не было: были разговоры по одному человеку и активность по одному
    человеку, поэтому «кто вообще писал сегодня» собиралось перебором учёток.

    Читается ПОД полным доступом и пишется в след: это чужая переписка, и
    «посмотрел ленту» должно быть отличимо от «ничего не делал».
    """
    _require(request, "admin.all_data.read")
    _audit_cross_tenant_read(request, "admin.chat_feed.read", None, limit=limit)
    storage = _services(request).storage
    items = []
    for row in storage.list_chat_feed(limit=limit):
        metadata = _json_value(row.get("metadata_json"), {})
        metadata = metadata if isinstance(metadata, dict) else {}
        items.append(
            {
                "user_id": row.get("user_id"),
                "display_name": row.get("display_name") or row.get("username") or row.get("user_id"),
                "username": row.get("username") or "",
                "preset_key": row.get("preset_key") or "",
                "status": row.get("status") or "",
                # Есть ли куда ответить: без чата кнопка ответа бессмысленна, и
                # человек должен видеть это заранее, а не после нажатия.
                "chat_id": str(metadata.get("chat_id") or ""),
                "last_content": row.get("last_content") or "",
                "last_role": row.get("last_role") or "",
                "last_at": row.get("last_at"),
                "last_conversation_id": row.get("last_conversation_id") or "",
                "message_count": int(row.get("message_count") or 0),
                "file_count": int(row.get("file_count") or 0),
            }
        )
    return {"items": items, "count": len(items)}


@router.post("/chats/{user_id}/reply")
async def reply_to_person(user_id: str, request: Request) -> dict[str, Any]:
    """Ответить человеку в его чат прямо из админки.

    Доставка — той же очередью, которой пользуются проактивные органы: мост
    дренирует её каждые пятнадцать секунд, поэтому отдельного транспорта не
    заводится. Сообщение помечается как ответ владельца — человек должен
    понимать, что это не Пятница сочинила.
    """
    _require(request, "admin.all_data.read")
    body = await _request_json(request)
    text = str(body.get("text") or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="Пустой ответ отправить нельзя")
    if len(text) > 4000:
        raise HTTPException(status_code=400, detail="Ответ длиннее 4000 знаков не поместится в сообщение")
    storage = _services(request).storage
    user = storage.get_user(user_id)
    if not user:
        raise HTTPException(status_code=404, detail="Пользователь не найден")
    metadata = _json_value(user.get("metadata_json"), {})
    metadata = metadata if isinstance(metadata, dict) else {}
    chat_id = str(metadata.get("chat_id") or "").strip()
    if not chat_id:
        raise HTTPException(status_code=409, detail="У этого человека нет чата — отвечать некуда")
    actor = request.state.actor
    # Ключ дедупа несёт время: два одинаковых ответа подряд — законное действие
    # («повторяю: жду отчёт»), и глушить второй нельзя.
    from friday.storage._base import new_id, utc_now

    queued = storage.enqueue_notification(
        user_id,
        chat_id,
        f"💬 Ответ от владельца:\n\n{text}",
        kind="owner_reply",
        dedup_key=f"owner_reply:{new_id('r')}:{utc_now()}",
    )
    _audit(
        request,
        "admin.chat.reply",
        target_type="user",
        target_id=user_id,
        after={"chars": len(text), "chat_id": chat_id, "queued": bool(queued), "by": actor.own_id},
    )
    return {"queued": bool(queued), "user_id": user_id, "chat_id": chat_id}


@router.get("/conversations")
async def list_all_conversations(
    request: Request,
    user_id: str | None = None,
    include_archived: bool = False,
    limit: int = Query(200, ge=1, le=1000),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    _require(request, "admin.all_data.read")
    target = _target_user(request, user_id)
    _audit_cross_tenant_read(request, "admin.conversations.read", target)
    storage = _services(request).storage
    items = storage.list_conversations(
        target,
        include_archived=include_archived,
        limit=limit,
        offset=offset,
    )
    # `count` used to be `len(items)` against a hard 1000-row cap, so a longer
    # history reported itself as exactly 1000 and the rest was unreachable.
    return {
        "user_id": target,
        "items": items,
        "count": len(items),
        "total": storage.count_conversations(target, include_archived=include_archived),
        "limit": limit,
        "offset": offset,
    }


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
        raise HTTPException(status_code=404, detail="Диалог не найден")
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
        raise HTTPException(status_code=404, detail="Диалог не найден")
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
    offset: int | None = Query(default=None, ge=0),
) -> dict[str, Any]:
    _require(request, "admin.all_data.read")
    if not _services(request).storage.get_conversation(conversation_id, user_id):
        raise HTTPException(status_code=404, detail="Диалог не найден")
    _audit_cross_tenant_read(request, "admin.messages.read", user_id, conversation_id=conversation_id)
    storage = _services(request).storage
    # Without an offset the window is the tail, as before. `total` is what the modal
    # lacked entirely: a 1200-message conversation returned 1000 rows and called that
    # the count, with no way to tell that the beginning had been dropped.
    total = storage.count_messages(conversation_id, user_id=user_id)
    effective_offset = max(0, total - min(limit, 1000)) if offset is None else offset
    items = storage.get_conversation_messages(
        conversation_id,
        user_id=user_id,
        limit=limit,
        offset=effective_offset,
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
    return {
        "conversation_id": conversation_id,
        "items": items,
        "count": len(items),
        "total": total,
        "limit": limit,
        "offset": effective_offset,
    }
