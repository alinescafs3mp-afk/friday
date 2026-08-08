"""Reminders organ — the first proactive organ.

Scans dated event entities (``entity_time``, the §11 timeline) for events
inside a lead window and enqueues a one-off push per event. This turns Friday
from a passive librarian into something that reaches out — while still writing
nothing to the knowledge graph. Delivery happens through the outbound
notification queue, drained by the Telegram bridge.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import timedelta

from friday.organs import (
    Organ,
    OrganWorker,
    ServiceContext,
    archive_tenant,
    in_quiet_hours,
    local_now,
    may_push_to,
    resolve_chat_id,
)
from friday.reminder_schedule import reminder_is_due, reminder_when_text
from friday.storage._graph import _bounded_visible_timeline_event_rows

LOGGER = logging.getLogger(__name__)


def _format_reminder(event: dict, today) -> str:
    name = str(event.get("name") or "событие").strip()
    when = reminder_when_text(event, today)
    return f"🔔 Напоминание: «{name}» — {when}."


def _belongs_to(event: dict, *, person: str, tenant: str) -> bool:
    """Кому напоминать об этом событии.

    Просьба человека («напомни завтра забрать пропуск») несёт отметку автора в
    источнике временной привязки — она принадлежит ему одному. Событие из
    документа отметки не имеет: это общий материал, и напоминает о нём хозяин
    архива, как и до общего архива.

    Замерено на живой настройке: без этого различения личная просьба уходила в
    чужой чат, а автор не получал ничего.
    """
    source = str(event.get("source") or "")
    if source.startswith("reminder:"):
        return source[len("reminder:") :] == person
    return person == tenant


async def scan_reminders(ctx: ServiceContext) -> None:
    settings = ctx.settings
    if not settings.reminders_enabled:
        return
    # Часы и календарная дата — местные: «сегодня»/«завтра» и тишина ночью суть
    # свойства дня человека, а не UTC. По UTC на боевой машине (МСК) событие
    # сегодняшнего дня подписывалось «завтра» после 21:00, а тихие часы
    # инвертировались на шесть часов из двадцати четырёх.
    now = local_now(settings)
    # Quiet hours gate enqueue, so nothing lands in the user's chat overnight;
    # a due event simply waits until the quiet window ends.
    if in_quiet_hours(now.hour, settings.quiet_hours_start, settings.quiet_hours_end):
        return
    allowed = settings.telegram_effective_allowed_chat_ids
    if not allowed:
        return
    today = now.date()
    # Exact-clock reminders missed during a short outage are allowed a bounded
    # catch-up window. Date-only events are filtered back to the historical
    # lead window by ``reminder_is_due`` below.
    start = (today - timedelta(days=7)).isoformat()
    end = (today + timedelta(days=max(0, settings.reminders_lead_days))).isoformat()

    enqueued = 0
    for user_id in ctx.storage.list_user_ids(active_only=True):
        chat_id = resolve_chat_id(ctx.storage, user_id)
        if not chat_id:
            continue
        # Deny-by-default, re-checked here (bridge re-checks again at send time).
        if not may_push_to(settings, ctx.storage, user_id, chat_id):
            continue
        tenant = archive_tenant(settings, user_id)
        for event in _bounded_visible_timeline_event_rows(
            ctx.storage,
            tenant,
            user_id,
            start=start,
            end=end,
        ):
            if not _belongs_to(event, person=user_id, tenant=tenant):
                continue
            if not reminder_is_due(event, now, lead_days=settings.reminders_lead_days):
                continue
            dedup_key = f"reminder:{event.get('entity_id')}:{event.get('occurred_at')}"
            if ctx.storage.enqueue_notification(
                user_id,
                chat_id,
                _format_reminder(event, today),
                kind="reminder",
                dedup_key=dedup_key,
            ):
                enqueued += 1
    if enqueued:
        LOGGER.info("Reminders organ queued %d event reminder(s)", enqueued)


class RemindersOrgan(Organ):
    name = "reminders"
    version = "1.0"

    def workers(self, ctx: ServiceContext) -> Sequence[OrganWorker]:
        return (
            OrganWorker(
                name="reminders_scan",
                run=scan_reminders,
                interval_sec=float(ctx.settings.reminders_poll_interval_sec),
                enabled=bool(ctx.settings.reminders_enabled),
                run_immediately=False,
                timeout_sec=300.0,
            ),
        )
