"""Chronicle organ — temporal presence / episodic memory.

The graph is mostly semantic (entities, relations); Friday does not really
live in time with the user. This organ adds the time axis:

* an **"on this day"** resurfacing push — knowledge captured on this calendar
  day in an earlier year ("📅 Год назад ты записал …"), the temporal
  proactivity that makes the system feel like a memory, not a database;
* an **episodic window** query — ``GET /api/chronicle?days=7`` returns the
  knowledge and events of the recent past ("что было на этой неделе").

Exercises all three JOP extension points. Like every organ, it reaches out but
writes nothing to the graph.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any

from fastapi import APIRouter, Query, Request

from friday.organs import (
    Organ,
    OrganWorker,
    ServiceContext,
    in_quiet_hours,
    local_now,
    may_push_to,
    resolve_chat_id,
)
from friday.permissions import CapabilityDefinition

LOGGER = logging.getLogger(__name__)

CHRONICLE_CAPABILITY = CapabilityDefinition(
    "chronicle.read",
    "Read the episodic timeline of recent and past-on-this-day knowledge",
    "chronicle",
    0,
    ("admin", "moderator", "user"),
    source="organ",
)


def _years_ago(iso: str, now: datetime) -> str:
    """A human 'N года назад' / 'в прошлом году' label from an ISO date."""
    year = 0
    try:
        year = int(str(iso)[:4])
    except (TypeError, ValueError):
        return ""
    delta = now.year - year
    if delta <= 0:
        return ""
    if delta == 1:
        return "год назад"
    return f"{delta} года назад" if 2 <= delta <= 4 else f"{delta} лет назад"


def build_window(storage, kg, user_id: str, *, days: int, settings=None) -> dict[str, Any]:
    """Episodic view of the recent past: knowledge captured + events in ``days``.

    ``settings`` задают пояс: границы окна событий — календарные даты, и по UTC
    вечером в Москве сегодняшний день из окна выпадал.
    """
    now = local_now(settings) if settings is not None else datetime.now().astimezone()
    since = (now - timedelta(days=max(1, days))).isoformat()
    recent = storage.list_recent_knowledge(user_id, since_iso=since, limit=50)
    events = storage.list_events_in_range(
        user_id,
        start=(now.date() - timedelta(days=max(1, days))).isoformat(),
        end=now.date().isoformat(),
        limit=50,
    )
    return {
        "days": days,
        "recent_knowledge": [
            {"title": k.get("title"), "kind": k.get("knowledge_kind"), "created_at": k.get("created_at")}
            for k in recent
        ],
        "events": [{"name": e.get("name"), "occurred_at": e.get("occurred_at")} for e in events],
    }


def build_on_this_day(storage, user_id: str, now: datetime) -> list[dict[str, Any]]:
    memories = storage.list_knowledge_on_this_day(
        user_id,
        month_day=now.strftime("%m-%d"),
        before_iso=now.date().isoformat(),
        limit=5,
    )
    return [
        {
            "title": m.get("title") or "Без названия",
            "created_at": m.get("created_at"),
            "ago": _years_ago(str(m.get("created_at") or ""), now),
        }
        for m in memories
    ]


def format_on_this_day(memories: list[dict[str, Any]]) -> str:
    lines = ["📅 В этот день"]
    for m in memories:
        ago = f" ({m['ago']})" if m["ago"] else ""
        lines.append(f"• {m['title']}{ago}")
    return "\n".join(lines)


async def chronicle_on_this_day(ctx: ServiceContext) -> None:
    settings = ctx.settings
    if not settings.chronicle_enabled:
        return
    now = local_now(settings)
    if in_quiet_hours(now.hour, settings.quiet_hours_start, settings.quiet_hours_end):
        return
    allowed = settings.telegram_effective_allowed_chat_ids
    if not allowed:
        return
    # One "on this day" push per calendar day per tenant.
    period_key = f"chronicle:{now.date().isoformat()}"

    enqueued = 0
    for user_id in ctx.storage.list_user_ids(active_only=True):
        chat_id = resolve_chat_id(ctx.storage, user_id)
        if not chat_id:
            continue
        if not may_push_to(settings, ctx.storage, user_id, chat_id):
            continue
        memories = build_on_this_day(ctx.storage, user_id, now)
        if not memories:  # nothing from a past year today — say nothing.
            continue
        if ctx.storage.enqueue_notification(
            user_id, chat_id, format_on_this_day(memories), kind="chronicle", dedup_key=period_key
        ):
            enqueued += 1
    if enqueued:
        LOGGER.info("Chronicle organ queued %d on-this-day memory push(es)", enqueued)


def _router() -> APIRouter:
    router = APIRouter(prefix="/api/chronicle", tags=["chronicle"])

    def _require(request: Request, capability: str):
        actor = request.state.actor
        request.app.state.auth_service.require(actor, capability)
        return actor

    @router.get("")
    async def get_chronicle(request: Request, days: int = Query(7, ge=1, le=365)) -> dict[str, Any]:
        actor = _require(request, "chronicle.read")
        state = request.app.state
        window = build_window(state.storage, state.kg, actor.user_id, days=days, settings=state.settings)
        # «В этот день» — про календарь человека: по UTC после 21:00 МСК хроника
        # показывала бы воспоминания завтрашнего числа.
        on_this_day = build_on_this_day(state.storage, actor.user_id, local_now(state.settings))
        return {"window": window, "on_this_day": on_this_day}

    return router


class ChronicleOrgan(Organ):
    name = "chronicle"
    version = "1.0"

    def capabilities(self) -> Sequence[CapabilityDefinition]:
        return (CHRONICLE_CAPABILITY,)

    def workers(self, ctx: ServiceContext) -> Sequence[OrganWorker]:
        return (
            OrganWorker(
                name="chronicle_on_this_day",
                run=chronicle_on_this_day,
                interval_sec=float(ctx.settings.chronicle_interval_sec),
                enabled=bool(ctx.settings.chronicle_enabled),
                run_immediately=False,
                timeout_sec=300.0,
            ),
        )

    def router(self) -> APIRouter | None:
        return _router()
