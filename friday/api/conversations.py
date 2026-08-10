"""HTTP routes for chat.

Moved verbatim out of the historical single-function ``create_app``: same paths,
methods, capabilities, bodies and responses, with the prefix lifted onto the
router. ``tests/test_route_inventory.py`` pins the published contract.
"""

from __future__ import annotations

import math
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request, Response

from friday.api.deps import _json_load, _parse_json_bool, _request_json, _require
from friday.api.projections import is_public_opaque_id, public_conversation_message
from friday.storage import normalize_conversation_mode

router = APIRouter(prefix="/api/conversations", tags=["chat"])

# Hard ceiling for a single export: Telegram documents are fine far above this,
# but a multi-year chat should not dump unbounded rows into memory. The file
# header always states when the window is the last N of a longer history.
EXPORT_MESSAGE_LIMIT = 500
_PUBLIC_ANSWER_MODES = frozenset(
    {"general_conversation", "mixed", "personal_knowledge", "personal_knowledge_missing", "structural"}
)
_PUBLIC_TRACE_STATUSES = frozenset({"below_limit", "discarded", "returned"})
_PUBLIC_TRACE_REASONS = frozenset(
    {"deleted", "deprecated_weak", "identifier_mismatch", "insufficient_evidence", "rerank_below_threshold"}
)


def _public_probability(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(parsed):
        return None
    return round(max(0.0, min(1.0, parsed)), 6)


def _resolve_conversation_ref(request: Request, actor: Any, conversation_id: str) -> str:
    """Map Telegram's «current chat = current conversation» to a real row id.

    Archive/delete/rename in Telegram have no place to put a conversation UUID
    (the person is already inside that chat). The bridge therefore calls these
    routes with the sentinel ``current``; only a signed telegram-bridge actor
    with a bound channel session may resolve it. A bare UUID still works for
    the API and for self-service tests that prove foreign ids return 404.
    """
    ref = str(conversation_id or "").strip()
    if ref != "current":
        return ref
    channel_chat_id = getattr(request.state, "bridge_chat_id", None)
    if actor.source == "telegram-bridge" and channel_chat_id:
        session = request.app.state.storage.get_channel_session(
            actor.own_id,
            "telegram",
            str(channel_chat_id),
        )
        if session:
            return str(session["conversation_id"])
    raise HTTPException(status_code=404, detail="Диалог не найден")


@router.post("/channel/reset", tags=["chat"])
async def reset_channel_conversation(request: Request) -> dict[str, Any]:
    actor = _require(request, "chat.use")
    body = await _request_json(request)
    channel = str(body.get("channel") or ("telegram" if actor.source == "telegram-bridge" else "api"))
    channel_id = str(body.get("channel_id") or getattr(request.state, "bridge_chat_id", ""))
    if not channel_id:
        raise HTTPException(status_code=400, detail="Нужен channel_id")
    cleared = request.app.state.storage.clear_channel_conversation(actor.own_id, channel, channel_id)
    return {"status": "reset", "cleared": cleared}


@router.get("/channel/why", tags=["chat"])
async def channel_answer_diagnostics(
    request: Request,
    channel: str | None = None,
    channel_id: str | None = None,
) -> dict[str, Any]:
    """Why the last answer in this chat came out the way it did.

    Answers the question the owner actually asks — "I definitely saved that note,
    why does it say there is nothing?" — with the query the agent really ran and the
    candidates it discarded, each with its reason. Those reasons
    (`identifier_mismatch`, `insufficient_evidence`, `deprecated_weak`) are computed
    on every query and were kept only for an admin explain call, so diagnosing it
    meant opening the admin panel on the host and retyping an approximation of a
    query the agent had rewritten — a different run, and often a different answer.
    """
    actor = _require(request, "conversations.read")
    storage = request.app.state.storage
    resolved_channel = str(channel or ("telegram" if actor.source == "telegram-bridge" else "api"))
    resolved_id = str(channel_id or getattr(request.state, "bridge_chat_id", ""))
    if not resolved_id:
        raise HTTPException(status_code=400, detail="Нужен channel_id")
    conversation_id = storage.get_channel_conversation(actor.own_id, resolved_channel, resolved_id)
    if not conversation_id:
        raise HTTPException(status_code=404, detail="В этом канале ещё нет диалога")
    messages = storage.get_conversation_messages(conversation_id, user_id=actor.own_id, limit=30)
    latest = next(
        (item for item in reversed(messages) if str(item.get("role")) == "assistant"),
        None,
    )
    if latest is None:
        raise HTTPException(status_code=404, detail="Пока нет ответа для объяснения")
    metadata = _json_load(latest.get("metadata_json"), {})
    metadata = metadata if isinstance(metadata, dict) else {}
    knowledge_cache: dict[str, dict[str, Any] | None] = {}

    def resolve_knowledge(value: Any) -> tuple[str, dict[str, Any]] | None:
        if not is_public_opaque_id(value, "ko"):
            return None
        if value not in knowledge_cache:
            knowledge_cache[value] = storage.get_knowledge_object(value, actor.user_id)
        item = knowledge_cache[value]
        return (value, item) if item else None

    citations: dict[str, str] = {}
    citation_value = metadata.get("knowledge_citations")
    if isinstance(citation_value, dict):
        for label, knowledge_id in list(citation_value.items())[:100]:
            if (
                isinstance(label, str)
                and label.startswith("K")
                and label[1:].isdigit()
                and 1 <= len(label[1:]) <= 4
                and resolve_knowledge(knowledge_id)
            ):
                citations[label] = str(knowledge_id)

    trace: list[dict[str, Any]] = []
    trace_value = metadata.get("retrieval_trace")
    if isinstance(trace_value, list):
        for candidate in trace_value[:10]:
            if not isinstance(candidate, dict):
                continue
            resolved = resolve_knowledge(candidate.get("id"))
            if not resolved:
                continue
            knowledge_id, knowledge = resolved
            projected: dict[str, Any] = {
                "id": knowledge_id,
                # Metadata is not authoritative for titles: an injected path or
                # private excerpt under `title` must not become a diagnostic.
                "title": str(knowledge.get("title") or "")[:500],
            }
            score = _public_probability(candidate.get("score"))
            if score is not None:
                projected["score"] = score
            status = candidate.get("status")
            if isinstance(status, str) and status in _PUBLIC_TRACE_STATUSES:
                projected["status"] = status
            reason = candidate.get("reason")
            if isinstance(reason, str) and reason in _PUBLIC_TRACE_REASONS:
                projected["reason"] = reason
            trace.append(projected)

    answer_mode = metadata.get("answer_mode")
    numeric_confidence = _public_probability(metadata.get("retrieval_confidence"))
    grounded = metadata.get("answer_grounded")
    try:
        knowledge_hits = (
            int(metadata.get("knowledge_hits") or 0)
            if not isinstance(metadata.get("knowledge_hits"), bool)
            else 0
        )
    except (TypeError, ValueError, OverflowError):
        knowledge_hits = 0
    search_query = metadata.get("search_query")
    return {
        "conversation_id": conversation_id,
        "created_at": latest.get("created_at"),
        "search_query": search_query[:2_000] if isinstance(search_query, str) else "",
        "answer_mode": answer_mode
        if isinstance(answer_mode, str) and answer_mode in _PUBLIC_ANSWER_MODES
        else "",
        "knowledge_hits": min(max(0, knowledge_hits), 2_147_483_647),
        "answer_grounded": grounded if isinstance(grounded, bool) else None,
        "retrieval_confidence": numeric_confidence,
        "citations": citations,
        "trace": trace,
    }


@router.post("/channel/mode", tags=["chat"])
async def set_channel_mode(request: Request) -> dict[str, Any]:
    actor = _require(request, "chat.use")
    body = await _request_json(request)
    channel = str(body.get("channel") or ("telegram" if actor.source == "telegram-bridge" else "api"))
    channel_id = str(body.get("channel_id") or getattr(request.state, "bridge_chat_id", ""))
    if not channel_id:
        raise HTTPException(status_code=400, detail="Нужен channel_id")
    try:
        mode = normalize_conversation_mode(str(body.get("mode") or "dialogue"))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    session = request.app.state.storage.get_channel_session(actor.own_id, channel, channel_id)
    if session:
        updated = request.app.state.storage.set_channel_mode(
            actor.own_id,
            channel,
            channel_id,
            mode,
        )
        return {"mode": mode, "session": updated, "conversation_created": False}
    conversation = request.app.state.storage.create_conversation(
        actor.own_id,
        title=f"{channel} {mode}",
        mode=mode,
    )
    request.app.state.storage.set_channel_conversation(
        actor.own_id,
        channel,
        channel_id,
        str(conversation["id"]),
        mode=mode,
    )
    return {
        "mode": mode,
        "session": request.app.state.storage.get_channel_session(actor.own_id, channel, channel_id),
        "conversation_created": True,
    }


@router.get("", tags=["chat"])
async def conversations(
    request: Request,
    include_archived: bool = False,
) -> dict[str, Any]:
    actor = _require(request, "conversations.read")
    items = request.app.state.storage.list_conversations(
        actor.own_id,
        include_archived=include_archived,
    )
    return {"items": items, "count": len(items)}


@router.get("/{conversation_id}/messages", tags=["chat"])
async def conversation_messages(
    conversation_id: str,
    request: Request,
    limit: int = Query(100, ge=1, le=1000),
) -> dict[str, Any]:
    actor = _require(request, "conversations.read")
    resolved = _resolve_conversation_ref(request, actor, conversation_id)
    if not request.app.state.storage.get_conversation(resolved, actor.own_id):
        raise HTTPException(status_code=404, detail="Диалог не найден")
    items = request.app.state.storage.get_conversation_messages(
        resolved,
        user_id=actor.own_id,
        limit=limit,
    )
    public_items = [
        public_conversation_message(
            item,
            storage=request.app.state.storage,
            resource_user_id=actor.user_id,
            resource_owner_id=actor.own_id,
        )
        for item in items
    ]
    return {"items": public_items, "count": len(public_items)}


def format_conversation_export(
    conversation: dict[str, Any],
    messages: list[dict[str, Any]],
    *,
    limit: int,
    truncated: bool,
) -> str:
    """Plain-text transcript: one line per message, role + time + text.

    No Telegram markup — this is a downloadable file, not a chat bubble.
    """
    conv_id = str(conversation.get("id") or "")
    title = str(conversation.get("title") or "").strip() or "без названия"
    lines = [
        "# Friday conversation export",
        f"# conversation_id: {conv_id}",
        f"# title: {title}",
        f"# messages: {len(messages)}",
    ]
    if truncated:
        lines.append(
            f"# note: показаны последние {limit} сообщений (потолок выгрузки; более ранние не включены)"
        )
    lines.append("")
    for row in messages:
        role = str(row.get("role") or "?").strip() or "?"
        created = str(row.get("created_at") or "").strip() or "?"
        content = str(row.get("content") or "")
        content = content.replace("\r\n", "\n").replace("\r", "\n")
        content = content.replace("\n", "\\n")
        lines.append(f"[{created}] {role}: {content}")
    lines.append("")
    return "\n".join(lines)


@router.get("/{conversation_id}/export", tags=["chat"])
async def export_conversation(
    conversation_id: str,
    request: Request,
    limit: int = Query(EXPORT_MESSAGE_LIMIT, ge=1, le=EXPORT_MESSAGE_LIMIT),
) -> Response:
    """G20: plain-text transcript of one conversation (self-service).

    Telegram is the primary consumer (bot sends the file via sendDocument),
    but the route is ordinary HTTP so tenant checks and foreign-id 404 can be
    pinned without mocking Telegram. Gate conversations.read — same as listing
    messages; no manage rights needed to read one's own chat.
    """
    actor = _require(request, "conversations.read")
    resolved = _resolve_conversation_ref(request, actor, conversation_id)
    conversation = request.app.state.storage.get_conversation(resolved, actor.own_id)
    if not conversation:
        raise HTTPException(status_code=404, detail="Диалог не найден")
    cap = max(1, min(int(limit), EXPORT_MESSAGE_LIMIT))
    items = request.app.state.storage.get_conversation_messages(
        resolved,
        user_id=actor.own_id,
        limit=cap,
    )
    # If we filled the window, earlier messages may exist — never silent-truncate.
    truncated = len(items) >= cap
    body = format_conversation_export(
        conversation,
        items,
        limit=cap,
        truncated=truncated,
    )
    filename = f"jericho-{resolved[:24]}.txt"
    return Response(
        content=body.encode("utf-8"),
        media_type="text/plain; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
        },
    )


@router.post("/{conversation_id}/archive", tags=["chat"])
async def archive_conversation(conversation_id: str, request: Request) -> dict[str, Any]:
    actor = _require(request, "conversations.manage")
    resolved = _resolve_conversation_ref(request, actor, conversation_id)
    body = await _request_json(request)
    archived = _parse_json_bool(body.get("archived"), field="archived", default=True)
    updated = request.app.state.storage.set_conversation_archived(resolved, actor.own_id, archived)
    if not updated:
        raise HTTPException(status_code=404, detail="Диалог не найден")
    return {"conversation": updated}


@router.patch("/{conversation_id}", tags=["chat"])
async def rename_conversation(conversation_id: str, request: Request) -> dict[str, Any]:
    """Self-service rename. Same tenant gate as archive/delete: only own rows."""
    actor = _require(request, "conversations.manage")
    resolved = _resolve_conversation_ref(request, actor, conversation_id)
    body = await _request_json(request)
    title = " ".join(str(body.get("title") or "").split()).strip()
    if not title:
        raise HTTPException(status_code=400, detail="Нужен title")
    updated = request.app.state.storage.set_conversation_title(resolved, actor.own_id, title)
    if not updated:
        raise HTTPException(status_code=404, detail="Диалог не найден")
    return {"conversation": updated}


@router.delete("/{conversation_id}", tags=["chat"])
async def delete_conversation(conversation_id: str, request: Request) -> dict[str, Any]:
    """Убрать разговор из списка. История при этом сохраняется.

    Метод остался DELETE — так его зовут обе панели и так его понимает человек
    («убрать отсюда»). Но сказанное в чате неудаляемо (требование владельца,
    2026-08-01), поэтому ответ говорит правду: `archived`, а не `deleted`, и
    называет число сохранённых сообщений. Обещать удаление, оставляя данные, —
    худший из вариантов: человек считает, что стёр, а оно лежит.
    """
    actor = _require(request, "conversations.manage")
    resolved = _resolve_conversation_ref(request, actor, conversation_id)
    report = request.app.state.storage.delete_conversation(resolved, actor.own_id)
    if not report.get("existed"):
        raise HTTPException(status_code=404, detail="Диалог не найден")
    return {
        "status": "archived",
        "report": report,
        "note": (
            f"Разговор убран из списка. Сообщений сохранено: {report.get('messages_kept', 0)} — "
            "переписка не удаляется."
        ),
    }
