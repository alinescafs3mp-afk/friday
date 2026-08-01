"""The one write into the operational journal that does not come from inside the backend.

The bridge is a separate process with its own SQLite, reaching the backend over signed
HTTP. That is deliberate — it holds the bot token and talks to the outside world — but
it means the events worth recording about it cannot be written the way a worker's are.

Those events are not hypothetical. A tunnel outage on this machine produced 295
long-polling failures across three days, and diagnosing it meant grepping a log file
and correlating timestamps by hand, because nothing the bridge experienced ever reached
``runtime_events``.

Narrow on purpose: the bridge may append events, from a fixed vocabulary, and nothing
else. The journal is capped, so an unconstrained writer could evict every real event by
flooding it.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from friday.api.deps import _request_json, _require_bridge

router = APIRouter(prefix="/api/events", tags=["events"])

# What the bridge is allowed to say. An allowlist rather than free text: the journal is
# a bounded resource shared with the backend's own events, and an event type nobody
# reads is noise that evicts something someone does.
BRIDGE_EVENT_TYPES = frozenset(
    {
        "bridge.poll_failed",
        "bridge.poll_recovered",
        "bridge.outbound_failed",
        "bridge.outbound_recovered",
        "bridge.dead_letter",
    }
)
MAX_PAYLOAD_KEYS = 12
MAX_VALUE_CHARS = 200


@router.post("")
async def record_bridge_event(request: Request) -> dict[str, Any]:
    _require_bridge(request)
    body = await _request_json(request)
    event_type = str(body.get("event_type") or "").strip()
    if event_type not in BRIDGE_EVENT_TYPES:
        raise HTTPException(status_code=400, detail="Неизвестный тип события моста")

    raw_payload = body.get("payload")
    payload: dict[str, Any] = {}
    if isinstance(raw_payload, dict):
        # Bounded on both axes. The bridge handles untrusted input from Telegram, so
        # whatever it forwards is treated as untrusted here too.
        for key, value in list(raw_payload.items())[:MAX_PAYLOAD_KEYS]:
            if value is None or isinstance(value, (bool, int, float)):
                payload[str(key)[:64]] = value
            else:
                payload[str(key)[:64]] = str(value)[:MAX_VALUE_CHARS]

    event_id = request.app.state.storage.record_event(event_type, payload)
    return {"id": event_id, "event_type": event_type}
