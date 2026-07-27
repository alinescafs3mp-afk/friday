"""HTTP routes for notifications.

Moved verbatim out of the historical single-function ``create_app``: same paths,
methods, capabilities, bodies and responses, with the prefix lifted onto the
router. ``tests/test_route_inventory.py`` pins the published contract.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query, Request

from jericho.api.deps import _request_json, _require_bridge
from jericho.config import JerichoSettings

router = APIRouter(prefix="/api/notifications", tags=["notifications"])


@router.get("/pending", tags=["notifications"])
async def notifications_pending(
    request: Request,
    limit: int = Query(20, ge=1, le=100),
) -> dict[str, Any]:
    _require_bridge(request)
    settings: JerichoSettings = request.app.state.settings
    storage = request.app.state.storage
    allowed = settings.telegram_effective_allowed_chat_ids
    items = []
    undeliverable: list[str] = []
    for row in storage.list_pending_notifications(limit=limit):
        # Defence in depth: never hand the bridge a de-allowlisted chat.
        try:
            if int(str(row.get("chat_id"))) not in allowed:
                undeliverable.append(str(row["id"]))
                continue
        except (TypeError, ValueError):
            undeliverable.append(str(row["id"]))
            continue
        items.append({"id": row["id"], "chat_id": row["chat_id"], "body": row["body"]})
    # Skipping was not enough: the row stayed pending with attempts=0 and no way
    # to ever leave the queue, and the queue is drained oldest-first with a limit
    # of 20 — twenty such rows and every later notification stops being delivered,
    # silently. Retiring them here keeps the queue moving.
    if undeliverable:
        storage.discard_notifications(undeliverable, reason="chat_not_allowed")
    return {"items": items, "count": len(items)}


@router.post("/ack", tags=["notifications"])
async def notifications_ack(request: Request) -> dict[str, Any]:
    _require_bridge(request)
    body = await _request_json(request)
    raw_sent = body.get("sent")
    raw_failed = body.get("failed")
    sent = [str(x) for x in raw_sent] if isinstance(raw_sent, list) else []
    failed = [str(x) for x in raw_failed] if isinstance(raw_failed, list) else []
    request.app.state.storage.mark_notifications(sent, failed)
    return {"sent": len(sent), "failed": len(failed)}
