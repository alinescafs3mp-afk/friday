"""HTTP routes for inbox.

Moved verbatim out of the historical single-function ``create_app``: same paths,
methods, capabilities, bodies and responses, with the prefix lifted onto the
router. ``tests/test_route_inventory.py`` pins the published contract.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request

from friday.api.deps import _audit, _request_json, _require
from friday.storage._intake import _bounded_public_inbox_card
from friday.storage.models import InboxStatus

router = APIRouter(prefix="/api/inbox", tags=["inbox"])


@router.get("", tags=["inbox"])
async def list_inbox(
    request: Request,
    status: str | None = None,
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    actor = _require(request, "inbox.read")
    try:
        status_enum = InboxStatus(status) if status else None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Недопустимый статус входящих") from exc
    items = request.app.state.storage.list_inbox(
        actor.user_id,
        status_enum,
        limit=limit,
        offset=offset,
    )
    public_items = [_bounded_public_inbox_card(item) for item in items]
    return {"items": public_items, "count": len(public_items)}


@router.post("/{inbox_id}/classify", tags=["inbox"])
async def classify_inbox(inbox_id: str, request: Request) -> dict[str, Any]:
    actor = _require(request, "inbox.review")
    body = await _request_json(request)
    try:
        status = InboxStatus(str(body.get("status") or "classified"))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Недопустимый статус входящих") from exc
    item = request.app.state.ingestion.classify_inbox_item(
        actor.user_id,
        inbox_id,
        status,
        entity_id=body.get("entity_id"),
        tags=body.get("tags") if isinstance(body.get("tags"), list) else None,
        notes=str(body.get("notes") or ""),
        reviewed_by=actor.own_id,
    )
    if not item:
        raise HTTPException(status_code=404, detail="Элемент входящих не найден")
    public_item = _bounded_public_inbox_card(item)
    _audit(request, "inbox.classify", "inbox", inbox_id, after=public_item)
    return {"item": public_item}
