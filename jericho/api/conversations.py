"""HTTP routes for chat.

Moved verbatim out of the historical single-function ``create_app``: same paths,
methods, capabilities, bodies and responses, with the prefix lifted onto the
router. ``tests/test_route_inventory.py`` pins the published contract.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request

from jericho.api.deps import _json_load, _parse_json_bool, _request_json, _require
from jericho.storage import normalize_conversation_mode

router = APIRouter(prefix="/api/conversations", tags=["chat"])


@router.post("/channel/reset", tags=["chat"])
async def reset_channel_conversation(request: Request) -> dict[str, Any]:
    actor = _require(request, "chat.use")
    body = await _request_json(request)
    channel = str(body.get("channel") or ("telegram" if actor.source == "telegram-bridge" else "api"))
    channel_id = str(body.get("channel_id") or getattr(request.state, "bridge_chat_id", ""))
    if not channel_id:
        raise HTTPException(status_code=400, detail="channel_id is required")
    cleared = request.app.state.storage.clear_channel_conversation(actor.user_id, channel, channel_id)
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
        raise HTTPException(status_code=400, detail="channel_id is required")
    conversation_id = storage.get_channel_conversation(actor.user_id, resolved_channel, resolved_id)
    if not conversation_id:
        raise HTTPException(status_code=404, detail="No conversation in this channel yet")
    messages = storage.get_conversation_messages(conversation_id, user_id=actor.user_id, limit=30)
    latest = next(
        (item for item in reversed(messages) if str(item.get("role")) == "assistant"),
        None,
    )
    if latest is None:
        raise HTTPException(status_code=404, detail="No answer to explain yet")
    metadata = _json_load(latest.get("metadata_json"), {})
    metadata = metadata if isinstance(metadata, dict) else {}
    return {
        "conversation_id": conversation_id,
        "created_at": latest.get("created_at"),
        "search_query": metadata.get("search_query", ""),
        "answer_mode": metadata.get("answer_mode", ""),
        "knowledge_hits": metadata.get("knowledge_hits", 0),
        "answer_grounded": metadata.get("answer_grounded"),
        "retrieval_confidence": metadata.get("retrieval_confidence"),
        "citations": metadata.get("knowledge_citations", {}),
        "trace": metadata.get("retrieval_trace", []),
    }


@router.post("/channel/mode", tags=["chat"])
async def set_channel_mode(request: Request) -> dict[str, Any]:
    actor = _require(request, "chat.use")
    body = await _request_json(request)
    channel = str(body.get("channel") or ("telegram" if actor.source == "telegram-bridge" else "api"))
    channel_id = str(body.get("channel_id") or getattr(request.state, "bridge_chat_id", ""))
    if not channel_id:
        raise HTTPException(status_code=400, detail="channel_id is required")
    try:
        mode = normalize_conversation_mode(str(body.get("mode") or "dialogue"))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    session = request.app.state.storage.get_channel_session(actor.user_id, channel, channel_id)
    if session:
        updated = request.app.state.storage.set_channel_mode(
            actor.user_id,
            channel,
            channel_id,
            mode,
        )
        return {"mode": mode, "session": updated, "conversation_created": False}
    conversation = request.app.state.storage.create_conversation(
        actor.user_id,
        title=f"{channel} {mode}",
        mode=mode,
    )
    request.app.state.storage.set_channel_conversation(
        actor.user_id,
        channel,
        channel_id,
        str(conversation["id"]),
        mode=mode,
    )
    return {
        "mode": mode,
        "session": request.app.state.storage.get_channel_session(actor.user_id, channel, channel_id),
        "conversation_created": True,
    }


@router.get("", tags=["chat"])
async def conversations(
    request: Request,
    include_archived: bool = False,
) -> dict[str, Any]:
    actor = _require(request, "conversations.read")
    items = request.app.state.storage.list_conversations(
        actor.user_id,
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
    if not request.app.state.storage.get_conversation(conversation_id, actor.user_id):
        raise HTTPException(status_code=404, detail="Conversation not found")
    items = request.app.state.storage.get_conversation_messages(
        conversation_id,
        user_id=actor.user_id,
        limit=limit,
    )
    return {"items": items, "count": len(items)}


@router.post("/{conversation_id}/archive", tags=["chat"])
async def archive_conversation(conversation_id: str, request: Request) -> dict[str, Any]:
    actor = _require(request, "conversations.manage")
    body = await _request_json(request)
    archived = _parse_json_bool(body.get("archived"), field="archived", default=True)
    updated = request.app.state.storage.set_conversation_archived(conversation_id, actor.user_id, archived)
    if not updated:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return {"conversation": updated}


@router.delete("/{conversation_id}", tags=["chat"])
async def delete_conversation(conversation_id: str, request: Request) -> dict[str, Any]:
    actor = _require(request, "conversations.manage")
    report = request.app.state.storage.delete_conversation(conversation_id, actor.user_id)
    if not report.get("existed"):
        raise HTTPException(status_code=404, detail="Conversation not found")
    return {"status": "deleted", "report": report}
